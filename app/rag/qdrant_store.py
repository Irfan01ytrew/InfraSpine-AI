"""Persistent Qdrant server adapter for vectors that should not be resident in the API process."""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Iterator, Sequence
from pathlib import Path

import numpy as np


class QdrantVectorStore:
    """On-disk Qdrant vectors with atomic alias cutover and resumable PostgreSQL rebuilds.

    PostgreSQL remains authoritative for text and access decisions. Qdrant stores only chunk IDs and vectors;
    filtered retrieval supplies the currently readable IDs from PostgreSQL and fetched content is authorized
    again before it is returned. Build collections remain separate from the active alias until completion.
    """

    backend = "qdrant"
    _WRITE_BATCH = 512

    def __init__(
        self,
        *,
        url: str,
        api_key: str | None,
        directory: Path,
        name: str,
        dimension: int,
        model_name: str,
        client=None,
    ) -> None:
        try:
            from qdrant_client import QdrantClient
            from qdrant_client import models
        except ImportError:
            raise RuntimeError("Install qdrant-client to use RAG_VECTOR_STORE=qdrant.") from None

        self._models = models
        self._client = client or QdrantClient(url=url, api_key=api_key or None, timeout=30)
        self._directory = directory.resolve()
        self._directory.mkdir(parents=True, exist_ok=True)
        self._manifest_path = self._directory / f"{name}.qdrant.json"
        suffix = re.sub(r"[^a-zA-Z0-9_-]", "_", name)[:64]
        self._alias = f"infraspine_{suffix}"
        self._build_alias = f"{self._alias}_building"
        self.dimension = dimension
        self.model_name = model_name
        self._manifest = self._read_manifest()
        self._rebuilding = False
        self._build_checkpoint = int(self._manifest.get("build_checkpoint") or 0)
        self._build_collection: str | None = self._manifest.get("build_collection")
        self._previous_collection: str | None = self._manifest.get("previous_collection")
        self._previous_model: str | None = self._manifest.get("previous_model")
        self._previous_dimension: int | None = self._manifest.get("previous_dimension")

        aliases = {item.alias_name: item.collection_name for item in self._client.get_aliases().aliases}
        if self._alias not in aliases:
            self._active_collection = self._new_collection_name("base")
            self._create_collection(self._active_collection)
            self._manifest["active_collection"] = self._active_collection
            self._manifest["model"] = model_name
            self._manifest["dimension"] = dimension
            self._manifest["distance"] = "cosine"
            self._manifest["normalization"] = "l2"
            self._client.update_collection_aliases(
                change_aliases_operations=[
                    models.CreateAliasOperation(
                        create_alias=models.CreateAlias(
                            collection_name=self._active_collection, alias_name=self._alias
                        )
                    )
                ]
            )
        else:
            self._active_collection = aliases[self._alias]

        # Resume a build alias left by a process interruption. It is never queried until atomic cutover.
        self._build_collection = aliases.get(self._build_alias, self._build_collection)
        if self._build_collection and not self._client.collection_exists(self._build_collection):
            self._build_collection = None
            self._build_checkpoint = 0
        active_matches = (
            self._manifest.get("active_collection") == self._active_collection
            and self._manifest.get("model") == model_name
            and self._manifest.get("dimension") == dimension
            and self._manifest.get("distance") == "cosine"
            and self._manifest.get("normalization") == "l2"
        )
        self._active_matches = active_matches
        self.needs_rebuild = not active_matches or self._build_collection is not None
        self._write_manifest()

    def _read_manifest(self) -> dict:
        try:
            data = json.loads(self._manifest_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_manifest(self) -> None:
        self._manifest.update(
            {
                "alias": self._alias,
                "active_collection": self._active_collection,
                "build_collection": self._build_collection,
                "build_checkpoint": self._build_checkpoint,
                "previous_collection": self._previous_collection,
                "previous_model": self._previous_model,
                "previous_dimension": self._previous_dimension,
            }
        )
        temporary = self._manifest_path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(self._manifest, handle, separators=(",", ":"), sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self._manifest_path)

    def _new_collection_name(self, kind: str) -> str:
        return f"{self._alias}_{kind}_{uuid.uuid4().hex[:16]}"

    def _create_collection(self, name: str) -> None:
        models = self._models
        self._client.create_collection(
            collection_name=name,
            vectors_config=models.VectorParams(
                size=self.dimension,
                distance=models.Distance.COSINE,
                memory=models.Memory.COLD,
            ),
            hnsw_config=models.HnswConfigDiff(memory=models.Memory.COLD),
            payload=models.PayloadStorageParams(memory=models.Memory.COLD),
        )
        self._client.create_payload_index(
            collection_name=name,
            field_name="chunk_id",
            field_schema=models.PayloadSchemaType.INTEGER,
            wait=True,
        )

    def _alias_target(self, alias: str) -> str | None:
        return next(
            (item.collection_name for item in self._client.get_aliases().aliases if item.alias_name == alias),
            None,
        )

    @property
    def rebuild_checkpoint(self) -> int:
        return self._build_checkpoint

    @property
    def rebuild_count(self) -> int:
        if not self._build_collection:
            return 0
        return int(self._client.count(self._build_alias, exact=True).count)

    def begin_rebuild(self) -> None:
        if self._rebuilding:
            raise RuntimeError("A Qdrant vector rebuild is already active.")
        build_target = self._alias_target(self._build_alias)
        if build_target and (
            self._manifest.get("build_model") != self.model_name
            or self._manifest.get("build_dimension") != self.dimension
            or self._manifest.get("build_distance") != "cosine"
            or self._manifest.get("build_normalization") != "l2"
        ):
            self._client.update_collection_aliases(
                change_aliases_operations=[
                    self._models.DeleteAliasOperation(
                        delete_alias=self._models.DeleteAlias(alias_name=self._build_alias)
                    )
                ]
            )
            self._client.delete_collection(build_target)
            build_target = None
            self._build_checkpoint = 0
        if not build_target:
            build_target = self._new_collection_name("build")
            self._create_collection(build_target)
            self._client.update_collection_aliases(
                change_aliases_operations=[
                    self._models.CreateAliasOperation(
                        create_alias=self._models.CreateAlias(
                            collection_name=build_target, alias_name=self._build_alias
                        )
                    )
                ]
            )
            self._build_checkpoint = 0
        self._build_collection = build_target
        self._manifest["build_model"] = self.model_name
        self._manifest["build_dimension"] = self.dimension
        self._manifest["build_distance"] = "cosine"
        self._manifest["build_normalization"] = "l2"
        self._rebuilding = True
        self._write_manifest()

    def add_rebuild_batch(self, ids: Sequence[int], vectors: np.ndarray) -> None:
        if not self._rebuilding or not self._build_collection:
            raise RuntimeError("No Qdrant vector rebuild is active.")
        self._upsert(self._build_alias, ids, vectors)
        if len(ids):
            self._build_checkpoint = max(self._build_checkpoint, max(int(item) for item in ids))
            self._write_manifest()

    def finish_rebuild(self) -> None:
        if not self._rebuilding or not self._build_collection:
            raise RuntimeError("No Qdrant vector rebuild is active.")
        old_collection = self._active_collection
        self._client.update_collection_aliases(
            change_aliases_operations=[
                self._models.DeleteAliasOperation(
                    delete_alias=self._models.DeleteAlias(alias_name=self._alias)
                ),
                self._models.DeleteAliasOperation(
                    delete_alias=self._models.DeleteAlias(alias_name=self._build_alias)
                ),
                self._models.CreateAliasOperation(
                    create_alias=self._models.CreateAlias(
                        collection_name=self._build_collection, alias_name=self._alias
                    )
                ),
            ]
        )
        self._active_collection = self._build_collection
        self._build_collection = None
        self._build_checkpoint = 0
        self._previous_collection = old_collection
        self._previous_model = self._manifest.get("model")
        self._previous_dimension = self._manifest.get("dimension")
        self._manifest["model"] = self.model_name
        self._manifest["dimension"] = self.dimension
        self._manifest["distance"] = "cosine"
        self._manifest["normalization"] = "l2"
        self._manifest.pop("build_model", None)
        self._manifest.pop("build_dimension", None)
        self._manifest.pop("build_distance", None)
        self._manifest.pop("build_normalization", None)
        self._rebuilding = False
        self._active_matches = True
        self.needs_rebuild = False
        self._write_manifest()

    def abort_rebuild(self) -> None:
        # Keep the build alias/checkpoint: a later invocation resumes idempotently from PostgreSQL's last ID.
        self._rebuilding = False
        self._active_matches = (
            self._manifest.get("active_collection") == self._active_collection
            and self._manifest.get("model") == self.model_name
            and self._manifest.get("dimension") == self.dimension
            and self._manifest.get("distance") == "cosine"
            and self._manifest.get("normalization") == "l2"
        )
        self.needs_rebuild = not self._active_matches or self._build_collection is not None
        self._write_manifest()

    def apply(
        self,
        *,
        remove_ids: Sequence[int] = (),
        add_ids: Sequence[int] = (),
        vectors: np.ndarray | None = None,
    ) -> None:
        if len(remove_ids):
            for start in range(0, len(remove_ids), self._WRITE_BATCH):
                points = [int(item) for item in remove_ids[start : start + self._WRITE_BATCH]]
                self._client.delete(
                    collection_name=self._alias,
                    points_selector=self._models.PointIdsList(points=points),
                    wait=True,
                )
        if len(add_ids):
            if vectors is None or vectors.shape != (len(add_ids), self.dimension):
                raise ValueError("vectors must have shape (len(add_ids), dimension)")
            self._upsert(self._alias, add_ids, vectors)

    def _upsert(self, collection: str, ids: Sequence[int], vectors: np.ndarray) -> None:
        if vectors.shape != (len(ids), self.dimension):
            raise ValueError("vectors must have shape (len(ids), dimension)")
        for start in range(0, len(ids), self._WRITE_BATCH):
            end = min(start + self._WRITE_BATCH, len(ids))
            points = [
                self._models.PointStruct(
                    id=int(ids[index]),
                    vector=np.asarray(vectors[index], dtype=np.float32).tolist(),
                    payload={"chunk_id": int(ids[index])},
                )
                for index in range(start, end)
            ]
            self._client.upsert(collection_name=collection, points=points, wait=True)

    def search(self, query: np.ndarray, k: int, allowed_ids: Sequence[int]) -> list[tuple[int, float]]:
        if not self._active_matches or not allowed_ids or k <= 0:
            return []
        response = self._client.query_points(
            collection_name=self._alias,
            query=np.asarray(query, dtype=np.float32).reshape(-1).tolist(),
            query_filter=self._models.Filter(
                must=[
                    self._models.FieldCondition(
                        key="chunk_id",
                        match=self._models.MatchAny(any=[int(item) for item in allowed_ids]),
                    )
                ]
            ),
            limit=k,
            with_payload=False,
            with_vectors=False,
        )
        return [(int(point.id), float(point.score)) for point in response.points]

    def search_all(self, query: np.ndarray, k: int) -> list[tuple[int, float]]:
        if not self._active_matches or k <= 0:
            return []
        response = self._client.query_points(
            collection_name=self._alias,
            query=np.asarray(query, dtype=np.float32).reshape(-1).tolist(),
            limit=k,
            with_payload=False,
            with_vectors=False,
        )
        return [(int(point.id), float(point.score)) for point in response.points]

    def iter_ids(self, batch_size: int = 1000) -> Iterator[int]:
        offset = None
        while True:
            points, next_offset = self._client.scroll(
                collection_name=self._alias,
                limit=batch_size,
                offset=offset,
                with_payload=False,
                with_vectors=False,
            )
            yield from (int(point.id) for point in points)
            if next_offset is None:
                break
            offset = next_offset

    def ids(self) -> set[int]:
        return set(self.iter_ids())

    def rebuild(self, ids: Sequence[int], vectors: np.ndarray) -> None:
        self.begin_rebuild()
        try:
            self.add_rebuild_batch(ids, vectors)
            self.finish_rebuild()
        except BaseException:
            self.abort_rebuild()
            raise

    def rollback(self) -> None:
        """Atomically repoint the active alias to the retained pre-cutover collection."""
        old = self._previous_collection
        if not old or not self._client.collection_exists(old):
            raise RuntimeError("No retained Qdrant collection is available for rollback.")
        current = self._active_collection
        self._client.update_collection_aliases(
            change_aliases_operations=[
                self._models.DeleteAliasOperation(
                    delete_alias=self._models.DeleteAlias(alias_name=self._alias)
                ),
                self._models.CreateAliasOperation(
                    create_alias=self._models.CreateAlias(collection_name=old, alias_name=self._alias)
                ),
            ]
        )
        self._active_collection, self._previous_collection = old, current
        self._manifest["model"] = self._previous_model
        self._manifest["dimension"] = self._previous_dimension
        self._previous_model = self.model_name
        self._previous_dimension = self.dimension
        self._active_matches = (
            self._manifest.get("model") == self.model_name
            and self._manifest.get("dimension") == self.dimension
            and self._manifest.get("distance") == "cosine"
            and self._manifest.get("normalization") == "l2"
        )
        self.needs_rebuild = not self._active_matches or self._build_collection is not None
        self._write_manifest()

    def count(self) -> int:
        return int(self._client.count(self._alias, exact=True).count)

    def describe(self) -> dict:
        return {
            "backend": self.backend,
            "index_type": "Qdrant HNSW with cold on-disk vectors and payloads",
            "vectors": self.rebuild_count if self._rebuilding else self.count(),
            "dimension": self.dimension,
            "model": self.model_name,
            "needs_rebuild": self.needs_rebuild,
            "rebuilding": self._rebuilding or self._build_collection is not None,
            "persisted": True,
            "snapshot_bytes": None,
            "change_log_bytes": None,
            "memory_bytes": None,
        }

    def close(self) -> None:
        self._client.close()
