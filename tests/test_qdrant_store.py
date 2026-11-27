import numpy as np
from qdrant_client import QdrantClient

from app.rag.qdrant_store import QdrantVectorStore


def _store(tmp_path, *, client=None):
    return QdrantVectorStore(
        url="http://127.0.0.1:6333",
        api_key=None,
        directory=tmp_path / "metadata",
        name="test_vectors",
        dimension=3,
        model_name="test-embedder-v1",
        client=client or QdrantClient(path=str(tmp_path / "qdrant")),
    )


def test_qdrant_filters_authorized_ids_and_persists_after_restart(tmp_path):
    store = _store(tmp_path)
    store.apply(
        add_ids=[11, 22, 33],
        vectors=np.asarray([[1, 0, 0], [0, 1, 0], [-1, 0, 0]], dtype=np.float32),
    )
    allowed = store.search(np.asarray([1, 0, 0], dtype=np.float32), 3, [22])
    assert [chunk_id for chunk_id, _score in allowed] == [22]
    assert abs(allowed[0][1]) < 1e-6
    assert store.search_all(np.asarray([1, 0, 0], dtype=np.float32), 1)[0][0] == 11
    store.close()

    restarted = _store(tmp_path)
    assert restarted.ids() == {11, 22, 33}
    assert restarted.count() == 3
    assert not restarted.needs_rebuild
    restarted.close()


def test_qdrant_rebuild_resumes_from_checkpoint_and_retains_rollback_index(tmp_path):
    store = _store(tmp_path)
    store.apply(add_ids=[1], vectors=np.asarray([[1, 0, 0]], dtype=np.float32))
    store.begin_rebuild()
    store.add_rebuild_batch([7], np.asarray([[0, 1, 0]], dtype=np.float32))
    assert store.rebuild_checkpoint == 7
    store.abort_rebuild()
    assert store.ids() == {1}
    store.close()

    resumed = _store(tmp_path)
    assert resumed.rebuild_checkpoint == 7
    assert resumed.needs_rebuild
    resumed.begin_rebuild()
    resumed.add_rebuild_batch([7], np.asarray([[0, 1, 0]], dtype=np.float32))
    resumed.finish_rebuild()
    assert resumed.ids() == {7}
    assert resumed.count() == 1

    resumed.rollback()
    assert resumed.ids() == {1}
    resumed.close()
