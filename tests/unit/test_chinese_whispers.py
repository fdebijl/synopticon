from __future__ import annotations

import numpy as np

from synopticon.cluster import graph
from synopticon.cluster.chinese_whispers import chinese_whispers


def _three_blobs(seed=0, per=15, dim=16, noise=0.1):
    rng = np.random.default_rng(seed)
    centers = np.eye(3, dim, dtype=np.float32)  # orthogonal, well separated
    pts = []
    for c in centers:
        pts.append(c + noise * rng.standard_normal((per, dim)).astype(np.float32))
    X = np.concatenate(pts).astype(np.float32)
    X /= np.linalg.norm(X, axis=1, keepdims=True)
    return X


def _canonical(labels):
    mapping = {}
    out = []
    for lab in labels.tolist():
        if lab not in mapping:
            mapping[lab] = len(mapping)
        out.append(mapping[lab])
    return out


def test_three_clusters():
    X = _three_blobs()
    idx, sims = graph.knn_graph(X, 10)
    labels = chinese_whispers(idx, sims, edge_threshold=0.5, iterations=30, seed=42)
    assert len(set(labels.tolist())) == 3


def test_determinism():
    X = _three_blobs(seed=3)
    idx, sims = graph.knn_graph(X, 10)
    a = chinese_whispers(idx, sims, 0.5, 30, seed=7)
    b = chinese_whispers(idx, sims, 0.5, 30, seed=7)
    assert _canonical(a) == _canonical(b)


def test_high_threshold_all_singletons():
    X = _three_blobs()
    idx, sims = graph.knn_graph(X, 10)
    labels = chinese_whispers(idx, sims, edge_threshold=0.9999, iterations=30, seed=42)
    assert len(set(labels.tolist())) == X.shape[0]
