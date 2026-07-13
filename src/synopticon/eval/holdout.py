"""Holdout evaluation and grid search over clustering/crossref parameters.

Reclusters in-memory via ``crossref._crossref_core`` — never writes
``cluster_runs`` rows. Metrics: recovery precision/recall on masked pairs,
pairwise F1, BCubed precision/recall/F, and a merge false-positive count.
"""

from __future__ import annotations

import csv
import itertools
import sqlite3
from pathlib import Path

import numpy as np

from ..cluster import crossref, graph
from ..config import Settings


def _stratified_mask(
    labeled: dict[int, tuple[str, int]], mask_fraction: float, seed: int
) -> set[int]:
    """Return the set of face_ids to mask (hide the label of)."""
    rng = np.random.default_rng(seed)
    by_person: dict[tuple[str, int], list[int]] = {}
    for fid, pk in labeled.items():
        by_person.setdefault(pk, []).append(fid)

    masked: set[int] = set()
    for pk, fids in by_person.items():
        n = len(fids)
        if n < 3:
            continue  # too few to mask
        k = int(round(mask_fraction * n))
        k = min(k, n - 1)  # keep at least one labeled
        if k <= 0:
            continue
        fids_sorted = sorted(fids)
        chosen = rng.choice(len(fids_sorted), size=k, replace=False)
        for i in chosen:
            masked.add(fids_sorted[int(i)])
    return masked


def _cluster_in_memory(conn: sqlite3.Connection, settings: Settings, label_map):
    face_ids, X = graph.load_fused(conn, settings)
    indices, sims = graph.build_or_load_graph(settings, face_ids, X)
    labels = crossref._cluster_labels(indices, sims, settings)
    face_meta = crossref._load_face_meta(conn)
    person_photos = crossref._load_person_photos(conn)
    result = crossref._crossref_core(
        face_ids, X, labels, label_map, settings, face_meta, person_photos
    )
    return face_ids, result


def _pairwise_and_bcubed(
    face_ids: np.ndarray, labels: np.ndarray, full_labels: dict[int, tuple[str, int]]
) -> dict:
    pos_of = {int(f): i for i, f in enumerate(face_ids.tolist())}
    items = [(fid, pk) for fid, pk in full_labels.items() if fid in pos_of]
    if len(items) < 2:
        return {
            "pairwise_precision": 1.0,
            "pairwise_recall": 1.0,
            "pairwise_f1": 1.0,
            "bcubed_precision": 1.0,
            "bcubed_recall": 1.0,
            "bcubed_f1": 1.0,
        }
    fids = np.array([f for f, _ in items])
    cl = np.array([labels[pos_of[f]] for f in fids])
    # Encode persons to ints.
    persons = {pk: i for i, pk in enumerate({pk for _, pk in items})}
    gt = np.array([persons[pk] for _, pk in items])

    # Pairwise.
    tp = fp = fn = 0
    n = len(fids)
    for i in range(n):
        same_c = cl[i + 1 :] == cl[i]
        same_p = gt[i + 1 :] == gt[i]
        tp += int(np.sum(same_c & same_p))
        fp += int(np.sum(same_c & ~same_p))
        fn += int(np.sum(~same_c & same_p))
    p = tp / (tp + fp) if (tp + fp) else 1.0
    r = tp / (tp + fn) if (tp + fn) else 1.0
    f1 = 2 * p * r / (p + r) if (p + r) else 0.0

    # BCubed.
    bp = np.zeros(n)
    br = np.zeros(n)
    for i in range(n):
        same_c = cl == cl[i]
        same_p = gt == gt[i]
        bp[i] = np.sum(same_c & same_p) / np.sum(same_c)
        br[i] = np.sum(same_c & same_p) / np.sum(same_p)
    bpm, brm = float(bp.mean()), float(br.mean())
    bf = 2 * bpm * brm / (bpm + brm) if (bpm + brm) else 0.0

    return {
        "pairwise_precision": p,
        "pairwise_recall": r,
        "pairwise_f1": f1,
        "bcubed_precision": bpm,
        "bcubed_recall": brm,
        "bcubed_f1": bf,
    }


def run_holdout(
    conn: sqlite3.Connection,
    settings: Settings,
    mask_fraction: float = 0.2,
    seed: int = 42,
) -> dict:
    full_labels = crossref.label_faces(conn, settings)
    masked = _stratified_mask(full_labels, mask_fraction, seed)
    masked_map = {fid: pk for fid, pk in full_labels.items() if fid not in masked}

    face_ids, result = _cluster_in_memory(conn, settings, masked_map)
    present = {int(f) for f in face_ids.tolist()}

    # Recovery on masked pairs.
    total = correct = predicted = 0
    for fid in masked:
        if fid not in present:
            continue
        total += 1
        pred = result.predicted_person.get(fid)
        if pred is None:
            continue
        predicted += 1
        if pred == full_labels[fid]:
            correct += 1
    recovery_recall = correct / total if total else 1.0
    recovery_precision = correct / predicted if predicted else 1.0

    clustering_metrics = _pairwise_and_bcubed(face_ids, result.labels, full_labels)

    merge_fp = len(result.merges)

    metrics = {
        "n_faces": int(face_ids.shape[0]),
        "n_masked": total,
        "recovery_precision": recovery_precision,
        "recovery_recall": recovery_recall,
        **clustering_metrics,
        "merge_false_positives": merge_fp,
        "edge_threshold": settings.clustering.edge_threshold,
        "knn_k": settings.clustering.knn_k,
        "algorithm": settings.clustering.algorithm,
        "majority": settings.crossref.majority,
        "assign_sim": settings.crossref.assign_sim,
    }
    _print_table(metrics)
    return metrics


def _print_table(metrics: dict) -> None:
    print("\nHoldout evaluation")
    print("-" * 40)
    for k, v in metrics.items():
        if isinstance(v, float):
            print(f"{k:24s} {v:.4f}")
        else:
            print(f"{k:24s} {v}")
    print("-" * 40)


def grid_search(
    conn: sqlite3.Connection,
    settings: Settings,
    grid: dict[str, list],
    mask_fraction: float = 0.2,
    seed: int = 42,
) -> list[dict]:
    """Grid over edge_threshold/knn_k/algorithm/majority/assign_sim → CSV."""
    keys = list(grid.keys())
    clustering_keys = {"edge_threshold", "knn_k", "algorithm"}
    rows: list[dict] = []

    for combo in itertools.product(*(grid[k] for k in keys)):
        s = settings.model_copy(deep=True)
        overrides = dict(zip(keys, combo))
        for k, v in overrides.items():
            if k in clustering_keys:
                setattr(s.clustering, k, v)
            else:
                setattr(s.crossref, k, v)
        metrics = run_holdout(conn, s, mask_fraction=mask_fraction, seed=seed)
        rows.append({**overrides, **metrics})

    out_path = Path(settings.storage.data_dir) / "eval_grid.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if rows:
        fieldnames = list(dict.fromkeys(k for r in rows for k in r))
        with out_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
    return rows
