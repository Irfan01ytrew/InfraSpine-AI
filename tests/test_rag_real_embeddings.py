"""Integration check with the real local embedding model (CPU). Skipped if the model can't be loaded
(e.g. offline on first run). The model is cached in data/models, shared with the app."""

from pathlib import Path

import numpy as np
import pytest

from app.rag.embeddings import SentenceTransformerEmbedder
from app.rag.vector_store import FaissVectorStore

MODEL = "sentence-transformers/all-MiniLM-L6-v2"
CACHE = Path(__file__).resolve().parent.parent / "data" / "models"

PASSAGES = [
    "Staff receive 25 days of paid annual leave per calendar year, plus public holidays.",
    "The datacenter cooling units are inspected every quarter by the facilities team.",
    "Expense reports must be submitted through the finance portal within two weeks.",
]


@pytest.fixture(scope="module")
def embedder():
    model = SentenceTransformerEmbedder(MODEL, 384, CACHE)
    try:
        model.warmup()
    except Exception as exc:  # pragma: no cover - depends on network on first run
        pytest.skip(f"embedding model unavailable: {type(exc).__name__}")
    return model


def test_real_embeddings_are_normalised_and_local(embedder):
    vectors = embedder.embed(PASSAGES)
    assert vectors.shape == (3, 384) and vectors.dtype == np.float32
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-4)


def test_semantic_retrieval_with_faiss(embedder, tmp_path):
    store = FaissVectorStore(tmp_path, 384, MODEL)
    store.apply(add_ids=[1, 2, 3], vectors=embedder.embed(PASSAGES))
    query = embedder.embed(["How many vacation days do employees get?"])[0]  # no word overlap with passage 1

    hits = store.search(query, 3, allowed_ids=[1, 2, 3])
    assert hits[0][0] == 1 and hits[0][1] > 0.4
    assert all(score < hits[0][1] - 0.2 for _, score in hits[1:])
    # Permission filter still applies with real vectors.
    assert 1 not in [i for i, _ in store.search(query, 3, allowed_ids=[2, 3])]
