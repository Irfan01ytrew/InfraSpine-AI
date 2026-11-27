from scripts.benchmark_vector_store import benchmark


def test_synthetic_vector_benchmark_measures_acl_scoped_search():
    report = benchmark(vector_count=1000, dimension=32, query_count=3, seed=13)

    assert report["vectors"] == 1000
    assert report["index_snapshot_bytes"] > 0
    assert report["index_float32_vector_bytes"] == 1000 * 32 * 4
    assert report["search"]["all_vectors"]["p95_ms"] >= 0
    assert report["search"]["10_percent_scope"]["p95_ms"] >= 0
