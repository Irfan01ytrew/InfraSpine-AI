"""Vector storage behind a small interface, so FAISS can later be swapped for Qdrant or pgvector.

PostgreSQL is the source of truth (chunk text + embedding bytes). The vector store is a
derived index: it may be stale or lost and can always be reconciled or rebuilt from the DB.
Vector IDs are `document_chunks.id` values, which are never reused.
"""

import hashlib
import importlib.util
import json
import logging
import os
import struct
import threading
import zlib
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

# On Windows, faiss-cpu wheels place native dependencies beside the Python package. Some wheel
# initializers add that DLL directory but release the returned handle before the SWIG module loads.
# Keep our own handle alive for the process so the packaged OpenBLAS/OpenMP libraries stay resolvable.
_faiss_dll_directory = None
if os.name == "nt":
    _faiss_spec = importlib.util.find_spec("faiss")
    if _faiss_spec and _faiss_spec.origin:
        _faiss_libs = Path(_faiss_spec.origin).resolve().parent.parent / "faiss_cpu.libs"
        if _faiss_libs.is_dir():
            _faiss_dll_directory = os.add_dll_directory(str(_faiss_libs))

import faiss
import numpy as np

logger = logging.getLogger(__name__)


class IndexLockedError(RuntimeError):
    """Another process already owns the vector index in this data directory."""


class IndexOwnerLock:
    """An OS-level exclusive lock that makes one process the only writer of a data directory's vector indexes.

    The API process owns the indexes; document workers write PostgreSQL only and never open them. A second
    owner (another API process on the same data directory, or `rag-rebuild-index` while the server runs)
    fails fast instead of corrupting the index files. Re-entrant within a process. The OS releases the lock
    when the process exits, so a crash never leaves a stale lock behind.
    """

    _held: dict[Path, list] = {}  # path -> [open file, reference count]
    _guard = threading.Lock()

    def __init__(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.path = (directory / "index.lock").resolve()
        self._owned = False

    def acquire(self) -> None:
        with self._guard:
            if self._owned:
                return
            held = self._held.get(self.path)
            if held is None:
                handle = self.path.open("a+b")
                try:
                    if os.name == "nt":
                        import msvcrt

                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    handle.close()
                    raise IndexLockedError(
                        f"Another process already owns the vector index in {self.path.parent}. Run a single API "
                        "process per data directory (document workers do not need the index)."
                    ) from None
                held = self._held[self.path] = [handle, 0]
            held[1] += 1
            self._owned = True

    def release(self) -> None:
        with self._guard:
            if not self._owned:
                return
            self._owned = False
            held = self._held.get(self.path)
            if held is None:
                return
            held[1] -= 1
            if held[1] == 0:
                handle = held[0]
                try:
                    if os.name == "nt":
                        import msvcrt

                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                finally:
                    handle.close()  # closing also releases a POSIX flock
                    del self._held[self.path]


class VectorStore(Protocol):
    backend: str  # configuration name of the implementation, e.g. "faiss"
    dimension: int
    needs_rebuild: bool  # set when persisted state was missing/invalid or a write failed

    def describe(self) -> dict:
        """Read-only status for administrators: index type, vector count, persistence. No vectors or document data."""
        ...

    def apply(self, *, remove_ids: Sequence[int] = (), add_ids: Sequence[int] = (), vectors: np.ndarray | None = None) -> None:
        """Atomically (from the caller's view) remove and add vectors, then persist."""
        ...

    def search(self, query: np.ndarray, k: int, allowed_ids: Sequence[int]) -> list[tuple[int, float]]:
        """Top-k (id, cosine similarity) restricted to `allowed_ids`."""
        ...

    def search_all(self, query: np.ndarray, k: int) -> list[tuple[int, float]]:
        """Top-k (id, cosine similarity) over every vector, best first. NOT authorised: the caller must
        check each ID against PostgreSQL before using it."""
        ...

    def ids(self) -> set[int]: ...

    def iter_ids(self, batch_size: int = 1000): ...

    @property
    def rebuild_checkpoint(self) -> int: ...

    @property
    def rebuild_count(self) -> int: ...

    def rebuild(self, ids: Sequence[int], vectors: np.ndarray) -> None: ...

    def begin_rebuild(self) -> None: ...

    def add_rebuild_batch(self, ids: Sequence[int], vectors: np.ndarray) -> None: ...

    def finish_rebuild(self) -> None: ...

    def abort_rebuild(self) -> None: ...

    def count(self) -> int: ...


class FaissVectorStore:
    """Exact cosine search (inner product on normalised vectors) persisted to local disk.

    On disk: a snapshot (`index.faiss` + `manifest.json`) and an append-only change log (`changes.log`).
    Each `apply` appends one checksummed record (removed IDs, added IDs and their vectors) and fsyncs it, so
    its cost follows the size of the change, not of the index. When the log outgrows a fraction of the
    snapshot, the next `apply` writes a new snapshot and starts an empty log. Loading replays the log over
    the snapshot. A torn or corrupt tail (a crash mid-append) is cut off with a warning; the change it held is
    restored from PostgreSQL by the startup reconcile, like any other change the index missed.
    """

    # Compact when the log exceeds this share of the snapshot (and at least the floor), which bounds both
    # disk use and replay time at load.
    COMPACT_RATIO = 0.25
    COMPACT_MIN_BYTES = 64 * 1024 * 1024
    backend = "faiss"

    def __init__(self, directory: Path, dimension: int, model_name: str, *, persist: bool = True) -> None:
        """`persist=False` makes an in-memory replica for an additional API process: it never reads or writes the
        owner's files (they may change underneath it) and is filled and kept current from PostgreSQL instead."""
        directory.mkdir(parents=True, exist_ok=True)
        self._persist_enabled = persist
        self.dimension = dimension
        self.model_name = model_name
        self._index_path = directory / "index.faiss"
        self._manifest_path = directory / "manifest.json"
        self._log_path = directory / "changes.log"
        # `_lock` guards the in-memory index: searches, and the brief mutation inside a write. `_write_lock`
        # serialises writers, including their disk I/O, which therefore never blocks searches.
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._generation: int | None = None  # snapshot the log belongs to; None = no snapshot on disk yet
        self._snapshot_bytes = 0
        self._log_bytes = 0  # end of the last valid record: appends start here
        self.needs_rebuild = False
        self._rebuilding = False
        self._index = self._load() if persist else self._empty()

    # ------------------------------------------------------------ persistence

    def _empty(self) -> faiss.IndexIDMap2:
        return faiss.IndexIDMap2(faiss.IndexFlatIP(self.dimension))

    def _load(self) -> faiss.IndexIDMap2:
        if not self._index_path.exists() and not self._manifest_path.exists():
            return self._empty()  # first run: nothing to rebuild unless the DB has chunks (reconcile decides)
        try:
            manifest = json.loads(self._manifest_path.read_text(encoding="utf-8"))
            data = self._index_path.read_bytes()
            if manifest.get("model") != self.model_name or manifest.get("dimension") != self.dimension:
                raise ValueError("index was built with a different embedding model")
            if hashlib.sha256(data).hexdigest() != manifest.get("sha256"):
                raise ValueError("index checksum mismatch")
            index = faiss.deserialize_index(np.frombuffer(data, dtype=np.uint8))
            del data
            if index.d != self.dimension or index.ntotal != manifest.get("count"):
                raise ValueError("index contents do not match manifest")
            self._generation = int(manifest.get("generation", 0))  # 0: written before the change log existed
            self._snapshot_bytes = self._index_path.stat().st_size
            replayed = self._replay(index)
        except Exception as exc:
            logger.warning("Vector index unusable error_type=%s; it will be rebuilt from the database", type(exc).__name__)
            self.needs_rebuild = True
            self._generation = None  # the next write starts a fresh snapshot
            return self._empty()
        logger.info("Vector index loaded (%s vectors, %s logged changes)", index.ntotal, replayed)
        return index

    def _replay(self, index: faiss.IndexIDMap2) -> int:
        """Apply the change log's valid records to the freshly loaded snapshot; returns how many."""
        if not self._log_path.exists():
            return 0
        raw = self._log_path.read_bytes()
        removed: set[int] = set()
        pending: dict[int, np.ndarray] = {}  # net additions, id -> vector (later records win)
        offset = records = 0
        while offset < len(raw):
            record = _decode_record(raw, offset, self.dimension)
            if record is None or record[0] != self._generation:
                break  # torn/corrupt tail, or a log left over from an older snapshot
            _, remove, add, vectors, offset = record
            for chunk_id in np.concatenate([remove, add]).tolist():
                pending.pop(chunk_id, None)
                removed.add(chunk_id)
            for row, chunk_id in enumerate(add.tolist()):
                pending[chunk_id] = vectors[row]
            records += 1
        if offset < len(raw):
            logger.warning(
                "Vector index change log has %s unreadable bytes after %s records; they are discarded and the "
                "index is repaired from the database by the next reconcile", len(raw) - offset, records,
            )
            with self._log_path.open("r+b") as log:
                log.truncate(offset)
                log.flush()
                os.fsync(log.fileno())
        self._log_bytes = offset
        if removed:
            index.remove_ids(faiss.IDSelectorBatch(np.fromiter(removed, dtype=np.int64, count=len(removed))))
        if pending:
            index.add_with_ids(
                np.ascontiguousarray(np.stack(list(pending.values())), dtype=np.float32),
                np.fromiter(pending.keys(), dtype=np.int64, count=len(pending)),
            )
        return records

    def _snapshot(self) -> None:
        """Write the whole index as a new snapshot and start an empty change log. Caller holds `_write_lock`,
        so the index cannot change meanwhile; searches keep running (writing only reads the index)."""
        generation = int.from_bytes(os.urandom(8), "little") or 1  # never 0, the pre-log generation
        digest = hashlib.sha256()
        temp = self._index_path.with_name(self._index_path.name + ".tmp")
        with temp.open("wb") as out:

            def sink(chunk: bytes) -> None:
                digest.update(chunk)
                out.write(chunk)

            faiss.write_index(self._index, faiss.PyCallbackIOWriter(sink, 1 << 22))  # streamed: no in-memory copy
            out.flush()
            os.fsync(out.fileno())
        manifest = {
            "model": self.model_name,
            "dimension": self.dimension,
            "count": int(self._index.ntotal),
            "sha256": digest.hexdigest(),
            "generation": generation,
        }
        # Index first, manifest second: a crash in between leaves a checksum mismatch, which is detected on
        # load and triggers a rebuild instead of serving bad data. The old log carries the old generation,
        # so a crash before it is emptied below only leaves records that the next load ignores.
        os.replace(temp, self._index_path)
        self._atomic_write(self._manifest_path, json.dumps(manifest).encode("utf-8"))
        self._snapshot_bytes = self._index_path.stat().st_size
        self._generation = generation
        self._atomic_write(self._log_path, b"")
        self._log_bytes = 0

    def _append(self, remove: np.ndarray, add: np.ndarray, vectors: np.ndarray) -> None:
        record = _encode_record(self._generation, remove, add, vectors)
        # Write at the end of the last valid record, overwriting any torn tail left by a failed append.
        with self._log_path.open("r+b" if self._log_path.exists() else "wb") as log:
            log.seek(self._log_bytes)
            log.write(record)
            log.truncate()
            log.flush()
            os.fsync(log.fileno())
        self._log_bytes += len(record)

    def _persist(self, remove: np.ndarray, add: np.ndarray, vectors: np.ndarray) -> None:
        if not self._persist_enabled:
            return
        limit = max(self.COMPACT_MIN_BYTES, self.COMPACT_RATIO * self._snapshot_bytes)
        if self._generation is None or self._log_bytes + _record_size(remove.size, add.size, self.dimension) > limit:
            self._snapshot()
        else:
            self._append(remove, add, vectors)

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        temp = path.with_name(path.name + ".tmp")
        with temp.open("wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)

    # ------------------------------------------------------------ operations

    def apply(self, *, remove_ids: Sequence[int] = (), add_ids: Sequence[int] = (), vectors: np.ndarray | None = None) -> None:
        with self._write_lock:
            try:
                remove = np.asarray(remove_ids, dtype=np.int64).reshape(-1)
                add = np.asarray(add_ids, dtype=np.int64).reshape(-1)
                if add.size and (vectors is None or vectors.shape != (add.size, self.dimension)):
                    raise ValueError("vectors must have shape (len(add_ids), dimension)")
                vectors = np.ascontiguousarray(
                    vectors if add.size else np.zeros((0, self.dimension)), dtype=np.float32
                )
                with self._lock:
                    if self._rebuilding:
                        raise RuntimeError("Cannot mutate the vector index during a rebuild")
                    # Removing the added IDs too makes adds idempotent. Removal scans the whole index, so it
                    # is skipped when none of the IDs is present (the usual case: chunk IDs are never reused).
                    drop = np.concatenate([remove, add])
                    drop = drop[np.isin(drop, faiss.vector_to_array(self._index.id_map))]
                    if drop.size:
                        self._index.remove_ids(faiss.IDSelectorBatch(drop))
                    if add.size:
                        self._index.add_with_ids(vectors, add)
                self._persist(remove, add, vectors)
            except Exception:
                self.needs_rebuild = True
                raise

    def search(self, query: np.ndarray, k: int, allowed_ids: Sequence[int]) -> list[tuple[int, float]]:
        allowed = np.asarray(allowed_ids, dtype=np.int64)
        with self._lock:
            if self._rebuilding or self.needs_rebuild or not allowed.size or self._index.ntotal == 0:
                return []
            params = faiss.SearchParameters(sel=faiss.IDSelectorBatch(allowed))
            scores, ids = self._index.search(
                np.ascontiguousarray(query, dtype=np.float32).reshape(1, -1), min(k, int(allowed.size)), params=params
            )
        return [(int(i), float(s)) for s, i in zip(scores[0], ids[0], strict=True) if i != -1]

    def search_all(self, query: np.ndarray, k: int) -> list[tuple[int, float]]:
        with self._lock:
            if self._rebuilding or self.needs_rebuild or k <= 0 or self._index.ntotal == 0:
                return []
            scores, ids = self._index.search(
                np.ascontiguousarray(query, dtype=np.float32).reshape(1, -1), min(k, int(self._index.ntotal))
            )
        return [(int(i), float(s)) for s, i in zip(scores[0], ids[0], strict=True) if i != -1]

    def ids(self) -> set[int]:
        with self._lock:
            return set(faiss.vector_to_array(self._index.id_map).tolist())

    def iter_ids(self, batch_size: int = 1000):
        # FAISS already holds its full index in memory; sorting preserves the ordered merge used by DB reconcile.
        yield from sorted(self.ids())

    @property
    def rebuild_checkpoint(self) -> int:
        return 0  # FAISS rebuilds are atomic snapshots; only Qdrant persists a resumable high-water mark.

    @property
    def rebuild_count(self) -> int:
        return self.count() if self._rebuilding else 0

    def rebuild(self, ids: Sequence[int], vectors: np.ndarray) -> None:
        self.begin_rebuild()
        try:
            self.add_rebuild_batch(ids, vectors)
            self.finish_rebuild()
        except BaseException:
            self.abort_rebuild()
            raise

    def begin_rebuild(self) -> None:
        """Start replacing the in-memory index while leaving its last durable snapshot untouched.

        A failed or interrupted rebuild can therefore restore the prior snapshot. Searches return no vector
        results until `finish_rebuild` commits the new snapshot; callers must still authorize content in PostgreSQL.
        """
        with self._write_lock, self._lock:
            if self._rebuilding:
                raise RuntimeError("A vector index rebuild is already in progress")
            self._index = self._empty()
            self._rebuilding = True
            self.needs_rebuild = True

    def add_rebuild_batch(self, ids: Sequence[int], vectors: np.ndarray) -> None:
        """Append one bounded batch to the in-progress rebuild without writing a change-log record."""
        ids_array = np.asarray(ids, dtype=np.int64).reshape(-1)
        vectors_array = np.asarray(vectors, dtype=np.float32)
        if vectors_array.shape != (ids_array.size, self.dimension):
            raise ValueError("vectors must have shape (len(ids), dimension)")
        if not ids_array.size:
            return
        with self._write_lock, self._lock:
            if not self._rebuilding:
                raise RuntimeError("No vector index rebuild is in progress")
            self._index.add_with_ids(np.ascontiguousarray(vectors_array), ids_array)

    def finish_rebuild(self) -> None:
        """Persist the completed replacement with one streamed snapshot write."""
        with self._write_lock:
            with self._lock:
                if not self._rebuilding:
                    raise RuntimeError("No vector index rebuild is in progress")
                if self._persist_enabled:
                    self._snapshot()
                self._rebuilding = False
                self.needs_rebuild = False

    def abort_rebuild(self) -> None:
        """Discard partial in-memory state and reload the last committed snapshot."""
        with self._write_lock, self._lock:
            if not self._rebuilding:
                return
            self._rebuilding = False
            self._generation = None
            self._snapshot_bytes = 0
            self._log_bytes = 0
            if self._persist_enabled:
                self.needs_rebuild = False
                self._index = self._load()
            else:  # a replica has no snapshot to fall back to: stay empty and rebuild on the next sync
                self.needs_rebuild = True
                self._index = self._empty()

    def count(self) -> int:
        with self._lock:
            return int(self._index.ntotal)

    def describe(self) -> dict:
        with self._lock:
            vectors = int(self._index.ntotal)
        return {
            "backend": self.backend,
            "index_type": "Exact cosine search (FAISS IndexFlatIP with ID map)",
            "vectors": vectors,
            "dimension": self.dimension,
            "model": self.model_name,
            "needs_rebuild": self.needs_rebuild,
            "rebuilding": self._rebuilding,
            "persisted": self._generation is not None,
            "replica": not self._persist_enabled,
            "snapshot_bytes": self._snapshot_bytes,
            "change_log_bytes": self._log_bytes,
            "memory_bytes": vectors * self.dimension * 4,  # float32 vectors held in RAM
        }


# Change-log record: header (magic, snapshot generation, #removed, #added), removed IDs (int64), added IDs
# (int64), added vectors (float32), then a CRC32 of everything before it, which detects torn writes.
_RECORD_MAGIC = b"FVL1"
_RECORD_HEADER = struct.Struct("<4sQII")
_RECORD_CRC = struct.Struct("<I")


def _record_size(n_remove: int, n_add: int, dimension: int) -> int:
    return _RECORD_HEADER.size + 8 * (n_remove + n_add) + 4 * n_add * dimension + _RECORD_CRC.size


def _encode_record(generation: int, remove: np.ndarray, add: np.ndarray, vectors: np.ndarray) -> bytes:
    body = b"".join(
        (
            _RECORD_HEADER.pack(_RECORD_MAGIC, generation, remove.size, add.size),
            remove.astype("<i8").tobytes(),
            add.astype("<i8").tobytes(),
            vectors.astype("<f4").tobytes(),
        )
    )
    return body + _RECORD_CRC.pack(zlib.crc32(body))


def _decode_record(raw: bytes, offset: int, dimension: int) -> tuple | None:
    """(generation, removed ids, added ids, vectors, end offset), or None if the record is incomplete or corrupt."""
    if offset + _RECORD_HEADER.size > len(raw):
        return None
    magic, generation, n_remove, n_add = _RECORD_HEADER.unpack_from(raw, offset)
    end = offset + _record_size(n_remove, n_add, dimension)
    if magic != _RECORD_MAGIC or end > len(raw):
        return None
    (crc,) = _RECORD_CRC.unpack_from(raw, end - _RECORD_CRC.size)
    if zlib.crc32(memoryview(raw)[offset : end - _RECORD_CRC.size]) != crc:
        return None
    position = offset + _RECORD_HEADER.size
    remove = np.frombuffer(raw, dtype="<i8", count=n_remove, offset=position)
    position += 8 * n_remove
    add = np.frombuffer(raw, dtype="<i8", count=n_add, offset=position)
    position += 8 * n_add
    vectors = np.frombuffer(raw, dtype="<f4", count=n_add * dimension, offset=position).reshape(n_add, dimension)
    return generation, remove, add, vectors, end
