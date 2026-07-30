"""Fused-embedding loading and exact cosine kNN graph construction.

Module-boundary rule: cluster/ never touches the network. This module reads
only the ``embeddings`` table (variant='orig' always — restored is advisory).
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import numpy as np

from ..config import Settings
from ..db import store
from ..progress import get_emitter


def _l2_normalize(mat: np.ndarray, axis: int = -1) -> np.ndarray:
    """L2-normalize along ``axis``; rows with zero norm are left untouched."""
    norms = np.linalg.norm(mat, axis=axis, keepdims=True)
    norms = np.where(norms == 0.0, 1.0, norms)
    return mat / norms


def load_fused(
    conn: sqlite3.Connection, settings: Settings
) -> tuple[np.ndarray, np.ndarray]:
    """Load per-model embeddings, fuse them, return ``(face_ids, X)``.

    - Reads ``variant='orig'`` only.
    - Keeps face_ids that have rows for *all* models present in the DB.
    - Per-model defensive L2-normalize, scale by ``fusion_weights`` (default
      1.0/model), concatenate in sorted-model-name order, then L2-normalize
      the concatenation.

    Returns ``face_ids`` (int64, ascending) and ``X`` (float32, ``(N, D)``).
    """
    emitter = get_emitter()
    # Stream the cursor rather than fetchall(): on a large library nearly all the
    # wall-clock of this phase is pulling the blobs out of SQLite, so a
    # fetchall() up front would do the waiting *before* the first progress event
    # and then race through the decode loop, reporting a phase that looks
    # instant after an unexplained multi-second freeze. The extra COUNT is an
    # index scan, cheap next to reading the vectors.
    total = conn.execute(
        "SELECT COUNT(*) FROM embeddings WHERE variant = 'orig'"
    ).fetchone()[0]
    cursor = conn.execute(
        "SELECT face_id, model, dim, vec FROM embeddings WHERE variant = 'orig'"
    )

    # face_id -> {model: vector}
    by_face: dict[int, dict[str, np.ndarray]] = {}
    models: set[str] = set()
    seen = 0
    for row in cursor:
        fid = int(row["face_id"])
        model = row["model"]
        vec = store.blob_to_vec(row["vec"]).reshape(int(row["dim"]))
        by_face.setdefault(fid, {})[model] = vec
        models.add(model)
        seen += 1
        if seen % 2000 == 0:
            emitter.progress("cluster.load", seen, total)
    emitter.progress("cluster.load", seen, max(seen, total))

    sorted_models = sorted(models)
    if not sorted_models:
        emitter.log("warning", "no embeddings found — run `synopticon extract` first")
        return np.empty(0, dtype=np.int64), np.empty((0, 0), dtype=np.float32)

    weights = dict(settings.clustering.fusion_weights)

    # Keep only faces having all available models.
    complete = sorted(
        fid for fid, mv in by_face.items() if all(m in mv for m in sorted_models)
    )
    emitter.log(
        "info",
        f"cluster.load: {len(complete)} face(s) with all {len(sorted_models)} model(s) "
        f"({', '.join(sorted_models)}); {len(by_face) - len(complete)} incomplete, skipped",
        phase="cluster.load",
    )
    if not complete:
        return np.empty(0, dtype=np.int64), np.empty((0, 0), dtype=np.float32)

    blocks: list[np.ndarray] = []
    for model in sorted_models:
        w = float(weights.get(model, 1.0))
        block = np.stack([by_face[fid][model] for fid in complete]).astype(np.float32)
        block = _l2_normalize(block, axis=1) * w
        blocks.append(block)

    fused = np.concatenate(blocks, axis=1).astype(np.float32)
    fused = _l2_normalize(fused, axis=1).astype(np.float32)
    face_ids = np.asarray(complete, dtype=np.int64)
    return face_ids, np.ascontiguousarray(fused)


def _knn_numpy(
    X: np.ndarray, k: int, chunk: int
) -> tuple[np.ndarray, np.ndarray]:
    n = X.shape[0]
    emitter = get_emitter()
    indices = np.empty((n, k), dtype=np.int32)
    sims = np.empty((n, k), dtype=np.float32)
    for start in range(0, n, chunk):
        emitter.progress("cluster.graph", start, n)
        end = min(start + chunk, n)
        block = X[start:end] @ X.T  # (chunk, n), cosine since X normalized
        # Exclude self.
        rows = np.arange(start, end)
        block[np.arange(end - start), rows] = -np.inf
        part = np.argpartition(-block, kth=k - 1, axis=1)[:, :k]
        part_sims = np.take_along_axis(block, part, axis=1)
        order = np.argsort(-part_sims, axis=1)  # descending, deterministic
        top_idx = np.take_along_axis(part, order, axis=1)
        top_sims = np.take_along_axis(part_sims, order, axis=1)
        indices[start:end] = top_idx.astype(np.int32)
        sims[start:end] = top_sims.astype(np.float32)
    emitter.progress("cluster.graph", n, n)
    return indices, sims


def _knn_faiss(X: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
    import faiss  # type: ignore

    n, d = X.shape
    emitter = get_emitter()
    index = faiss.IndexFlatIP(d)
    index.add(np.ascontiguousarray(X, dtype=np.float32))
    # Retrieve k+1 to drop self.
    sims_all, idx_all = index.search(np.ascontiguousarray(X, dtype=np.float32), k + 1)
    indices = np.empty((n, k), dtype=np.int32)
    sims = np.empty((n, k), dtype=np.float32)
    for i in range(n):
        if i % 5000 == 0:
            emitter.progress("cluster.graph", i, n)
        row_idx = idx_all[i]
        row_sim = sims_all[i]
        mask = row_idx != i
        keep_idx = row_idx[mask][:k]
        keep_sim = row_sim[mask][:k]
        # Pad if fewer than k returned.
        if keep_idx.shape[0] < k:
            pad = k - keep_idx.shape[0]
            keep_idx = np.concatenate([keep_idx, np.full(pad, -1, dtype=keep_idx.dtype)])
            keep_sim = np.concatenate([keep_sim, np.full(pad, -np.inf, dtype=keep_sim.dtype)])
        indices[i] = keep_idx.astype(np.int32)
        sims[i] = keep_sim.astype(np.float32)
    # Re-sort deterministically to match numpy path exactly.
    order = np.argsort(-sims, axis=1)
    indices = np.take_along_axis(indices, order, axis=1)
    sims = np.take_along_axis(sims, order, axis=1)
    emitter.progress("cluster.graph", n, n)
    return indices, sims


def knn_graph(
    X: np.ndarray, k: int, chunk: int = 2048
) -> tuple[np.ndarray, np.ndarray]:
    """Exact self-excluded cosine top-k. Returns ``(indices, sims)`` (N,K).

    Uses faiss ``IndexFlatIP`` when importable, numpy fallback otherwise;
    results are sorted identically (descending sim) so both paths agree.
    """
    n = X.shape[0]
    emitter = get_emitter()
    kk = min(k, n - 1) if n > 1 else 0
    if kk <= 0:
        return (
            np.empty((n, 0), dtype=np.int32),
            np.empty((n, 0), dtype=np.float32),
        )
    try:
        import faiss  # noqa: F401

        emitter.log("info", f"cluster.graph: exact top-{kk} kNN over {n} faces (faiss)",
                    phase="cluster.graph")
        return _knn_faiss(X, kk)
    except Exception:
        emitter.log("info", f"cluster.graph: exact top-{kk} kNN over {n} faces (numpy)",
                    phase="cluster.graph")
        return _knn_numpy(X, kk, chunk)


def _signature(face_ids: np.ndarray, k: int, fusion_weights: dict) -> str:
    h = hashlib.sha1()
    h.update(np.asarray(face_ids, dtype="<i8").tobytes())
    h.update(repr(int(k)).encode())
    h.update(repr(sorted(fusion_weights.items())).encode())
    return h.hexdigest()[:16]


def save_graph(
    data_dir: Path,
    face_ids: np.ndarray,
    k: int,
    fusion_weights: dict,
    indices: np.ndarray,
    sims: np.ndarray,
) -> Path:
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    sig = _signature(face_ids, k, fusion_weights)
    path = data_dir / f"graph_{sig}.npz"
    np.savez(
        path,
        face_ids=np.asarray(face_ids, dtype=np.int64),
        indices=indices,
        sims=sims,
        k=np.int64(k),
    )
    return path


def load_graph(
    data_dir: Path, face_ids: np.ndarray, k: int, fusion_weights: dict
) -> tuple[np.ndarray, np.ndarray] | None:
    data_dir = Path(data_dir)
    sig = _signature(face_ids, k, fusion_weights)
    path = data_dir / f"graph_{sig}.npz"
    if not path.is_file():
        return None
    data = np.load(path)
    stored = data["face_ids"]
    if stored.shape != face_ids.shape or not np.array_equal(stored, face_ids):
        return None
    return data["indices"], data["sims"]


def build_or_load_graph(
    settings: Settings, face_ids: np.ndarray, X: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Return the cached kNN graph for this (face_ids, k, weights) or compute it.

    Retunes that only change edge_threshold/algorithm reuse the cached matmul.
    """
    k = settings.clustering.knn_k
    weights = dict(settings.clustering.fusion_weights)
    emitter = get_emitter()
    cached = load_graph(settings.storage.data_dir, face_ids, k, weights)
    if cached is not None:
        emitter.log(
            "info", "cluster.graph: reusing cached kNN graph (same faces, k and weights)",
            phase="cluster.graph",
        )
        return cached
    indices, sims = knn_graph(X, k)
    save_graph(settings.storage.data_dir, face_ids, k, weights, indices, sims)
    return indices, sims
