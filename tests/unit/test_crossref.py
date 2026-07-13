from __future__ import annotations

import numpy as np

from synopticon.cluster import crossref
from synopticon.config import load_settings


def _norm(v):
    v = np.asarray(v, dtype=np.float32)
    return v / np.linalg.norm(v)


def test_iou_label_matching(db_helpers):
    h = db_helpers
    h.insert_photo("personal", 1)
    # our face normalized [0.1,0.1,0.3,0.3]
    f_match = h.insert_face("personal", 1, 100, 100, 200, 200)
    # our face far away -> no syno match
    f_nomatch = h.insert_face("personal", 1, 700, 700, 100, 100)
    h.insert_syno_face("personal", 1, 1, 5, (0.1, 0.1, 0.3, 0.3))
    h.commit()

    labels = crossref.label_faces(h.conn, load_settings())
    assert labels.get(f_match) == ("personal", 5)
    assert f_nomatch not in labels


def test_photo_level_fallback(db_helpers):
    h = db_helpers
    h.insert_photo("personal", 2)
    f = h.insert_face("personal", 2, 100, 100, 200, 200)  # exactly one face
    h.insert_person_photo("personal", 9, 2)  # exactly one tagged person, no syno_faces
    h.commit()

    labels = crossref.label_faces(h.conn, load_settings())
    assert labels.get(f) == ("personal", 9)


def _core_inputs(face_specs, labels, label_map):
    """face_specs: list of (face_id, vec). Returns args for _crossref_core."""
    face_ids = np.array([fid for fid, _ in face_specs], dtype=np.int64)
    X = np.stack([_norm(v) for _, v in face_specs]).astype(np.float32)
    meta = {
        fid: {"space": "personal", "photo_id": 1000 + i, "bbox_norm": [0.1, 0.1, 0.2, 0.2]}
        for i, (fid, _) in enumerate(face_specs)
    }
    return face_ids, X, np.asarray(labels), label_map, meta


def test_majority_mapping_and_assign_split():
    settings = load_settings()
    # One cluster (labels all 0): 3 labeled A, one near unlabeled (assign),
    # one far unlabeled (low_confidence).
    specs = [
        (1, [1, 0, 0, 0]),
        (2, [1, 0, 0, 0]),
        (3, [1, 0, 0, 0]),
        (4, [1, 0, 0, 0]),          # near -> assign
        (5, [0.4, 0.9, 0, 0]),      # cos ~0.4 -> low_confidence
    ]
    label_map = {1: ("personal", 100), 2: ("personal", 100), 3: ("personal", 100)}
    face_ids, X, labels, lm, meta = _core_inputs(specs, [0] * 5, label_map)
    result = crossref._crossref_core(
        face_ids, X, labels, lm, settings, meta, person_photos=set()
    )
    assert result.clusters[0].mapped_person == ("personal", 100)
    kinds = {a["payload"]["face_id"]: a["kind"] for a in result.assigns}
    assert kinds[4] == "assign"
    assert kinds[5] == "low_confidence"


def test_no_mapping_below_min_labeled():
    settings = load_settings()
    specs = [(1, [1, 0, 0, 0]), (2, [1, 0, 0, 0])]
    label_map = {1: ("personal", 100), 2: ("personal", 100)}  # only 2 labeled < 3
    face_ids, X, labels, lm, meta = _core_inputs(specs, [0, 0], label_map)
    result = crossref._crossref_core(
        face_ids, X, labels, lm, settings, meta, person_photos=set()
    )
    assert result.clusters[0].mapped_person is None
    assert result.assigns == []


def test_merge_from_split_person():
    settings = load_settings()
    # 3 person A + 2 person B in one cluster: both >= merge_vote_fraction 0.3.
    specs = [(i, [1, 0, 0, 0]) for i in range(1, 6)]
    label_map = {
        1: ("personal", 10),
        2: ("personal", 10),
        3: ("personal", 10),
        4: ("personal", 20),
        5: ("personal", 20),
    }
    face_ids, X, labels, lm, meta = _core_inputs(specs, [0] * 5, label_map)
    result = crossref._crossref_core(
        face_ids, X, labels, lm, settings, meta, person_photos=set()
    )
    assert len(result.merges) == 1
    m = result.merges[0]
    pair = tuple(sorted([m["person_key_a"], m["person_key_b"]]))
    assert pair == (("personal", 10), ("personal", 20))


def test_run_clustering_dedup_across_runs(db_helpers, tmp_settings):
    h = db_helpers
    h.insert_person("personal", 100, "Alice")
    # 4 faces of Alice in distinct photos; 3 labeled via syno_faces, 1 unlabeled.
    for i in range(4):
        h.insert_photo("personal", i)
        h.insert_face("personal", i, 100, 100, 200, 200)  # -> [0.1,0.1,0.3,0.3]
    # face_ids are 1..4
    for fid in range(1, 5):
        h.insert_embedding(fid, "arcface", [1.0, 0.0, 0.0, 0.0])
    for si, photo in enumerate([0, 1, 2]):
        h.insert_syno_face("personal", si + 1, photo, 100, (0.1, 0.1, 0.3, 0.3))
    h.commit()

    run1 = crossref.run_clustering(h.conn, tmp_settings)
    run2 = crossref.run_clustering(h.conn, tmp_settings)
    assert run1 != run2

    rows = h.conn.execute(
        "SELECT payload_json FROM review_queue WHERE kind IN ('assign','low_confidence')"
    ).fetchall()
    import json

    identities = [
        (json.loads(r["payload_json"])["face_id"], json.loads(r["payload_json"])["person_id"])
        for r in rows
    ]
    # The single unlabeled face (id 4) -> exactly one assign identity, no dupes.
    assert len(identities) == len(set(identities))
    assert (4, 100) in identities
