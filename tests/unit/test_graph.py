from __future__ import annotations

import numpy as np

from synopticon.cluster import graph


def _normalize(x):
    return x / np.linalg.norm(x, axis=1, keepdims=True)


def _brute_force(X, k):
    S = X @ X.T
    np.fill_diagonal(S, -np.inf)
    n = X.shape[0]
    idx = np.empty((n, k), dtype=np.int64)
    sims = np.empty((n, k), dtype=np.float32)
    for i in range(n):
        order = np.argsort(-S[i])[:k]
        idx[i] = order
        sims[i] = S[i, order]
    return idx, sims


def test_knn_matches_brute_force():
    rng = np.random.default_rng(0)
    X = _normalize(rng.standard_normal((40, 8)).astype(np.float32))
    k = 5
    idx, sims = graph.knn_graph(X, k)
    bidx, bsims = _brute_force(X, k)
    assert np.array_equal(idx.astype(np.int64), bidx)
    assert np.allclose(sims, bsims, atol=1e-5)


def test_knn_numpy_path_matches_brute_force():
    rng = np.random.default_rng(1)
    X = _normalize(rng.standard_normal((25, 6)).astype(np.float32))
    idx, sims = graph._knn_numpy(X, 4, chunk=8)
    bidx, bsims = _brute_force(X, 4)
    assert np.array_equal(idx.astype(np.int64), bidx)
    assert np.allclose(sims, bsims, atol=1e-5)


def test_load_fused(db_helpers, tmp_settings):
    h = db_helpers
    h.insert_photo("personal", 1)
    f1 = h.insert_face("personal", 1, 10, 10, 20, 20)
    f2 = h.insert_face("personal", 1, 40, 40, 20, 20)
    f3 = h.insert_face("personal", 1, 70, 70, 20, 20)
    # f1, f2 have both models; f3 only one -> excluded.
    h.insert_embedding(f1, "arcface", [1.0, 0.0, 0.0])
    h.insert_embedding(f1, "adaface", [0.0, 1.0, 0.0])
    h.insert_embedding(f2, "arcface", [0.0, 1.0, 0.0])
    h.insert_embedding(f2, "adaface", [1.0, 0.0, 0.0])
    h.insert_embedding(f3, "arcface", [1.0, 0.0, 0.0])
    h.commit()

    face_ids, X = graph.load_fused(h.conn, tmp_settings)
    assert list(face_ids) == sorted([f1, f2])
    assert X.shape == (2, 6)  # 3 + 3 concatenated
    assert np.allclose(np.linalg.norm(X, axis=1), 1.0, atol=1e-5)


def test_graph_cache_roundtrip(tmp_path):
    rng = np.random.default_rng(2)
    face_ids = np.arange(10, dtype=np.int64)
    X = _normalize(rng.standard_normal((10, 4)).astype(np.float32))
    idx, sims = graph.knn_graph(X, 3)
    graph.save_graph(tmp_path, face_ids, 3, {}, idx, sims)
    loaded = graph.load_graph(tmp_path, face_ids, 3, {})
    assert loaded is not None
    lidx, lsims = loaded
    assert np.array_equal(lidx, idx)
    assert np.allclose(lsims, sims)
    # Different k -> cache miss.
    assert graph.load_graph(tmp_path, face_ids, 5, {}) is None
