"""Chinese Whispers graph clustering on a thresholded kNN graph.

Deterministic given a seed. Singleton faces (no edges above threshold) keep
their own label.
"""

from __future__ import annotations

import numpy as np
from scipy import sparse

from ..progress import get_emitter

#: Node visits between `cluster.labels` progress events. The label-propagation
#: loop is per-node Python, so calling the emitter on every node would show up in
#: the runtime; every 2000 visits is still sub-second granularity.
_PROGRESS_EVERY = 2000


def _build_adjacency(
    indices: np.ndarray, sims: np.ndarray, edge_threshold: float
) -> sparse.csr_matrix:
    n = indices.shape[0]
    if n == 0 or indices.shape[1] == 0:
        return sparse.csr_matrix((n, n), dtype=np.float32)
    rows = np.repeat(np.arange(n), indices.shape[1])
    cols = indices.reshape(-1)
    weights = sims.reshape(-1).astype(np.float32)
    mask = (weights >= edge_threshold) & (cols >= 0)
    rows, cols, weights = rows[mask], cols[mask], weights[mask]
    adj = sparse.coo_matrix((weights, (rows, cols)), shape=(n, n)).tocsr()
    # Symmetrize keeping the max edge weight in either direction.
    adj = adj.maximum(adj.T)
    return adj.tocsr()


def chinese_whispers(
    indices: np.ndarray,
    sims: np.ndarray,
    edge_threshold: float,
    iterations: int,
    seed: int,
) -> np.ndarray:
    """Return an integer label per node (order matches ``indices`` rows).

    Each node adopts the label with maximum summed edge weight among its
    neighbours; ties break toward the smallest label id for determinism.
    """
    n = indices.shape[0]
    if n == 0:
        return np.empty(0, dtype=np.int64)

    adj = _build_adjacency(indices, sims, edge_threshold)
    labels = np.arange(n, dtype=np.int64)

    indptr, adj_idx, adj_w = adj.indptr, adj.indices, adj.data
    rng = np.random.default_rng(seed)

    emitter = get_emitter()
    emitter.log(
        "info",
        f"cluster.labels: chinese whispers over {n} nodes, {adj.nnz // 2} edges "
        f"(threshold {edge_threshold}), {iterations} iteration(s)",
        phase="cluster.labels",
    )
    visits = 0
    budget = iterations * n

    for it in range(iterations):
        order = rng.permutation(n)
        for node in order:
            visits += 1
            if visits % _PROGRESS_EVERY == 0:
                emitter.progress("cluster.labels", visits, budget, iteration=it + 1)
            start, end = indptr[node], indptr[node + 1]
            if start == end:
                continue  # isolated node keeps its own label
            neigh = adj_idx[start:end]
            w = adj_w[start:end]
            neigh_labels = labels[neigh]
            # Sum weights per neighbour label.
            uniq, inv = np.unique(neigh_labels, return_inverse=True)
            totals = np.zeros(uniq.shape[0], dtype=np.float64)
            np.add.at(totals, inv, w)
            best = totals.max()
            # Smallest label among those achieving the max (deterministic).
            candidates = uniq[totals >= best - 1e-12]
            labels[node] = candidates.min()

    emitter.progress("cluster.labels", budget, budget)
    emitter.log(
        "info",
        f"cluster.labels: {len(np.unique(labels))} label(s) after propagation",
        phase="cluster.labels",
    )
    return labels
