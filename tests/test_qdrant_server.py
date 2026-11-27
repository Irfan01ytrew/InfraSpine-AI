"""Optional actual-server Qdrant integration tests (never run against the local application index)."""

from __future__ import annotations

import os
import uuid

import numpy as np
import pytest
from qdrant_client import QdrantClient

from app.rag.qdrant_store import QdrantVectorStore


def test_server_persistence_rebuild_cutover_rollback_and_acl_revocation(tmp_path):
    url = os.environ.get("QDRANT_TEST_URL")
    if not url:
        pytest.skip("set QDRANT_TEST_URL to an isolated Qdrant test service")
    api_key = os.environ.get("QDRANT_TEST_API_KEY")
    namespace = uuid.uuid4().hex[:16]
    store_args = {
        "url": url,
        "api_key": api_key,
        "directory": tmp_path / "manifest",
        "name": f"verify_{namespace}",
        "dimension": 3,
        "model_name": "synthetic-test-embedder-v1",
    }

    client = QdrantClient(url=url, api_key=api_key, timeout=15)
    try:
        client.get_collections()  # fail immediately with a useful connection/authentication error
    except Exception:
        client.close()
        raise
    store = QdrantVectorStore(**store_args, client=client)
    store.apply(
        add_ids=[101, 202],
        vectors=np.asarray([[1, 0, 0], [0, 1, 0]], dtype=np.float32),
    )
    assert store.count() == 2
    assert [item[0] for item in store.search(np.asarray([1, 0, 0]), 3, [202])] == [202]
    assert store.search(np.asarray([1, 0, 0]), 3, []) == []  # permission revocation is applied on each query

    manifest_dir = store_args["directory"]
    store.close()
    restarted = QdrantVectorStore(
        **{**store_args, "directory": manifest_dir},
        client=QdrantClient(url=url, api_key=api_key, timeout=15),
    )
    assert restarted.ids() == {101, 202}
    assert restarted.count() == 2

    restarted.begin_rebuild()
    restarted.add_rebuild_batch([303], np.asarray([[0, 0, 1]], dtype=np.float32))
    restarted.abort_rebuild()  # simulate interruption after a persisted batch/checkpoint
    restarted.close()

    resumed = QdrantVectorStore(
        **{**store_args, "directory": manifest_dir},
        client=QdrantClient(url=url, api_key=api_key, timeout=15),
    )
    assert resumed.needs_rebuild
    assert resumed.rebuild_checkpoint == 303
    resumed.begin_rebuild()
    resumed.add_rebuild_batch([303], np.asarray([[0, 0, 1]], dtype=np.float32))  # duplicate upsert is idempotent
    assert resumed.rebuild_count == 1
    resumed.finish_rebuild()
    assert resumed._alias_target(resumed._alias) == resumed._active_collection
    assert resumed.ids() == {303}
    assert resumed.count() == 1

    resumed.rollback()
    assert resumed.ids() == {101, 202}
    assert resumed.count() == 2

    resumed.apply(
        remove_ids=[101],
        add_ids=[202],
        vectors=np.asarray([[1, 0, 0]], dtype=np.float32),
    )
    assert resumed.count() == 1  # duplicate ID updates in place instead of creating another vector
    assert resumed.ids() == {202}
    assert [item[0] for item in resumed.search(np.asarray([1, 0, 0]), 3, [202])] == [202]
    assert resumed.search(np.asarray([1, 0, 0]), 3, [101]) == []  # revoked/deleted IDs cannot be returned
    resumed.close()
