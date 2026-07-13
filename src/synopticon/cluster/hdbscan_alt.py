"""HDBSCAN alternative clustering on a precomputed sparse distance matrix.

Kept behind ``clustering.algorithm='hdbscan'`` for comparison. Its ``-1``
noise labels double as a low-confidence signal; callers treat noise faces as
per-face singletons but keep the noise flag.

Note: hdbscan's sparse ``metric='precomputed'`` path rejects graphs with more
than one connected component, so we cluster each component independently.
"""

from __future__ import annotations

import numpy as np
from scipy import sparse
from scipy.sparse.csgraph import connected_components

from ..config import Settings


def _build_distance(indices: np.ndarray, sims: np.ndarray) -> sparse.csr_matrix:
    n = indices.shape[0]
    if n == 0 or indices.shape[1] == 0:
        return sparse.csr_matrix((n, n), dtype=np.float64)
    rows = np.repeat(np.arange(n), indices.shape[1])
    cols = indices.reshape(-1)
    sim = sims.reshape(-1)
    mask = (cols >= 0) & np.isfinite(sim)
    rows, cols, sim = rows[mask], cols[mask], sim[mask]
    dist = (1.0 - sim).astype(np.float64)
    # Keep distances strictly positive so zero-distance is not confused with
    # "no edge" in the sparse representation.
    dist = np.clip(dist, 1e-6, None)
    mat = sparse.coo_matrix((dist, (rows, cols)), shape=(n, n)).tocsr()
    # Symmetrize taking the smaller (closer) distance.
    mat = mat.minimum(mat.T)
    # minimum() over structural zeros yields 0 for missing edges; restore the
    # union structure by taking element-wise min only where both present, else
    # the existing value. maximum of structures then min of data:
    upper = mat
    lower = mat.T
    combined = upper.maximum(lower)  # union structure, max value
    # Where both directions exist we want the min; emulate via building from
    # the union with min data. Simpler: rebuild from COO of the union.
    combined = combined.tocoo()
    return sparse.csr_matrix(
        (combined.data, (combined.row, combined.col)), shape=(n, n)
    )


def cluster_hdbscan(
    indices: np.ndarray, sims: np.ndarray, settings: Settings
) -> np.ndarray:
    """Return integer labels with ``-1`` preserved as noise."""
    import hdbscan  # local import; heavy optional dep

    n = indices.shape[0]
    if n == 0:
        return np.empty(0, dtype=np.int64)

    dist = _build_distance(indices, sims)
    min_cluster_size = max(2, settings.clustering.min_cluster_size)
    labels = np.full(n, -1, dtype=np.int64)

    ncomp, comp = connected_components(dist, directed=False)
    next_label = 0
    for c in range(ncomp):
        members = np.where(comp == c)[0]
        if members.shape[0] < min_cluster_size:
            continue  # too small -> noise
        sub = dist[members][:, members]
        clusterer = hdbscan.HDBSCAN(
            metric="precomputed",
            min_cluster_size=min_cluster_size,
            min_samples=min_cluster_size,
        )
        sub_labels = clusterer.fit_predict(sub)
        for local, lab in zip(members, sub_labels):
            labels[local] = -1 if lab == -1 else next_label + int(lab)
        if sub_labels.max() >= 0:
            next_label += int(sub_labels.max()) + 1

    return labels
