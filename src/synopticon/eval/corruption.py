"""Corrupted-tag evaluation: how well does crossref catch a tag Synology got wrong?

There is no ground truth for a wrong Synology tag, so this makes some. A sample
of correct tags on listed persons is moved to a different listed person in the
same space, faces are grouped again in memory, and every corrupted tag is
checked for a reassign back to its true person. A baseline run without
corruption separates the false alarms the corruption causes from the
disagreements that were already there (some of them Synology's real mistakes).

Never writes to the database.
"""

from __future__ import annotations

import numpy as np

from ..cluster import crossref, graph
from ..config import Settings
from ..db import Connection

PersonKey = crossref.PersonKey
MODES = ("random", "lookalike")


def corrupt(
    label_map: dict[int, PersonKey],
    candidates: list[int],
    face_ids: np.ndarray,
    X: np.ndarray,
    known_persons: set[PersonKey],
    fraction: float,
    seed: int,
    mode: str,
) -> dict[int, PersonKey]:
    """Pick ``fraction`` of ``candidates`` and a wrong listed person for each.

    ``random`` draws uniformly from the other listed persons in the face's
    space; ``lookalike`` takes the one whose centroid is closest to the face,
    which is the mistake a face recogniser is likeliest to make.
    """
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, not {mode!r}")
    rng = np.random.default_rng(seed)
    pos_of = {int(f): i for i, f in enumerate(face_ids.tolist())}
    by_person: dict[PersonKey, list[int]] = {}
    for fid, pk in label_map.items():
        if pk in known_persons and fid in pos_of:
            by_person.setdefault(pk, []).append(pos_of[fid])
    persons = sorted(by_person)
    centroids = np.stack([X[by_person[pk]].mean(axis=0) for pk in persons])
    centroids /= np.linalg.norm(centroids, axis=1, keepdims=True)

    pool = sorted(candidates)
    k = int(round(fraction * len(pool)))
    chosen = sorted(int(f) for f in rng.choice(pool, size=k, replace=False)) if k else []
    out: dict[int, PersonKey] = {}
    for fid in chosen:
        true = label_map[fid]
        others = [j for j, pk in enumerate(persons) if pk != true and pk[0] == true[0]]
        if not others:
            continue
        if mode == "random":
            j = others[int(rng.integers(len(others)))]
        else:
            j = others[int(np.argmax(centroids[others] @ X[pos_of[fid]]))]
        out[fid] = persons[j]
    return out


def _photos_after(
    person_photos: set[tuple[str, int, int]],
    label_map: dict[int, PersonKey],
    corrupted: dict[int, PersonKey],
    face_meta: dict[int, dict],
) -> set[tuple[str, int, int]]:
    """Photo-level tags as they would read had Synology made these mistakes.

    Left alone, the true person's photo tag would make crossref skip the very
    reassign it should propose ("photo already tagged with this person").
    """
    out = set(person_photos)
    tagged_on_photo: dict[tuple[str, int, PersonKey], int] = {}
    for fid, pk in label_map.items():
        meta = face_meta.get(fid)
        if meta is not None:
            key = (meta["space"], meta["photo_id"], pk)
            tagged_on_photo[key] = tagged_on_photo.get(key, 0) + 1
    for fid, wrong in corrupted.items():
        true = label_map[fid]
        photo = face_meta[fid]["photo_id"]
        if tagged_on_photo.get((true[0], photo, true), 0) <= 1:
            out.discard((true[0], true[1], photo))
        out.add((wrong[0], wrong[1], photo))
    return out


def _reassigns(result: crossref.ClusterResult) -> dict[int, tuple[PersonKey, str]]:
    """face_id -> (proposed person, 'nearest' | 'cluster')."""
    return {
        r["payload"]["face_id"]: (
            r["person_key"], "nearest" if "nearest_person" in r["payload"] else "cluster"
        )
        for r in result.reassigns
    }


def run_corruption(
    conn: Connection,
    settings: Settings,
    fraction: float = 0.05,
    seed: int = 42,
    modes: tuple[str, ...] = MODES,
) -> dict:
    face_ids, X = graph.load_fused(conn, settings)
    indices, sims = graph.build_or_load_graph(settings, face_ids, X)
    labels = crossref._cluster_labels(indices, sims, settings)
    present = {int(f) for f in face_ids.tolist()}
    all_labels, all_syno = crossref.label_faces_with_ids(conn, settings)
    label_map = {f: pk for f, pk in all_labels.items() if f in present}
    syno_face_ids = {f: s for f, s in all_syno.items() if f in present}
    face_meta = crossref._load_face_meta(conn)
    person_photos = crossref._load_person_photos(conn)
    known = crossref.load_known_persons(conn)

    def run(lm, photos):
        return _reassigns(crossref._crossref_core(
            face_ids, X, labels, lm, settings, face_meta, photos,
            syno_face_ids=syno_face_ids, known_persons=known,
        ))

    candidates = [f for f, pk in label_map.items() if pk in known and f in syno_face_ids]
    baseline = run(label_map, person_photos)
    metrics: dict = {
        "n_faces": int(face_ids.shape[0]),
        "n_candidates": len(candidates),
        "baseline_listed_reassigns": sum(
            1 for f in baseline if label_map.get(f) in known
        ),
    }
    for mode in modes:
        corrupted = corrupt(label_map, candidates, face_ids, X, known, fraction, seed, mode)
        lm = {**label_map, **corrupted}
        found = run(lm, _photos_after(person_photos, label_map, corrupted, face_meta))

        caught = {f for f in corrupted if found.get(f, (None,))[0] == label_map[f]}
        misdirected = {f for f in corrupted if f in found and f not in caught}
        false_alarms = {
            f for f, hit in found.items()
            if f not in corrupted and label_map.get(f) in known and baseline.get(f) != hit
        }
        raised = len(caught) + len(misdirected) + len(false_alarms)
        n = len(corrupted)
        metrics[mode] = {
            "corrupted": n,
            "caught": len(caught),
            "caught_by_cluster": sum(1 for f in caught if found[f][1] == "cluster"),
            "caught_by_nearest": sum(1 for f in caught if found[f][1] == "nearest"),
            "misdirected": len(misdirected),
            "new_false_alarms": len(false_alarms),
            "recall": len(caught) / n if n else 1.0,
            "precision": len(caught) / raised if raised else 1.0,
        }
    _print_table(metrics)
    return metrics


def _print_table(metrics: dict) -> None:
    print("\nCorrupted-tag evaluation")
    print("-" * 44)
    for k, v in metrics.items():
        if isinstance(v, dict):
            print(f"[{k}]")
            for kk, vv in v.items():
                print(f"  {kk:22s} {vv:.4f}" if isinstance(vv, float) else f"  {kk:22s} {vv}")
        else:
            print(f"{k:24s} {v}")
    print("-" * 44)
