"""Additional API processes run as in-memory vector index replicas (RAG_INDEX_ROLE=auto/replica)."""

import base64

import numpy as np
import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.rag.isolation import run_isolated
from app.rag.vector_store import FaissVectorStore, IndexOwnerLock
from tests import isolation_targets

DIM = 4
FILES = ("index.faiss", "manifest.json", "changes.log")


def _unit(*rows: list[float]) -> np.ndarray:
    vectors = np.asarray(rows, dtype=np.float32)
    return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)


def test_replica_store_searches_from_memory_and_never_writes_files(tmp_path):
    replica = FaissVectorStore(tmp_path, DIM, "model", persist=False)
    replica.apply(add_ids=[1, 2], vectors=_unit([1, 0, 0, 0], [0, 1, 0, 0]))
    replica.rebuild([3, 4], _unit([0, 0, 1, 0], [0, 0, 0, 1]))
    replica.apply(remove_ids=[4])

    assert replica.ids() == {3}
    assert replica.search(_unit([0, 0, 1, 0])[0], 1, [3])[0][0] == 3
    assert not any((tmp_path / name).exists() for name in FILES)
    assert replica.describe()["replica"] is True and replica.describe()["persisted"] is False


def test_replica_ignores_owner_files_and_stays_empty_after_an_aborted_rebuild(tmp_path):
    owner = FaissVectorStore(tmp_path, DIM, "model")
    owner.apply(add_ids=[7], vectors=_unit([1, 1, 0, 0]))
    assert (tmp_path / "manifest.json").exists()

    replica = FaissVectorStore(tmp_path, DIM, "model", persist=False)
    assert replica.count() == 0  # filled from PostgreSQL by the sync loop, never from files that may be changing
    replica.begin_rebuild()
    replica.add_rebuild_batch([9], _unit([0, 1, 1, 0]))
    replica.abort_rebuild()
    assert replica.count() == 0 and replica.needs_rebuild  # the next sync retries the rebuild
    assert FaissVectorStore(tmp_path, DIM, "model").ids() == {7}  # owner files untouched


def test_auto_role_becomes_replica_only_while_another_process_owns_the_index(tmp_path):
    def claim(requested: str) -> str:
        return run_isolated(isolation_targets.claim_index_role, (str(tmp_path), requested), timeout_seconds=60, memory_bytes=0)

    owner = IndexOwnerLock(tmp_path)
    owner.acquire()
    try:
        assert claim("owner") == "locked"  # default: a second owner still fails fast
        assert claim("auto") == "replica"
        assert claim("replica") == "replica"
    finally:
        owner.release()
    assert claim("auto") == "owner"


def test_replica_roles_require_faiss():
    with pytest.raises(ValidationError, match="RAG_INDEX_ROLE"):
        Settings(_env_file=None, rag_index_role="auto", rag_vector_store="qdrant",
                 database_url="postgresql+asyncpg://u@localhost/db", master_encryption_key=base64.b64encode(b"k" * 32).decode())
