from __future__ import annotations

import numpy as np
import pytest

from synopticon.eval import corruption

_A, _B, _C = ("personal", 1), ("personal", 2), ("personal", 3)
_VECS = {_A: [1.0, 0.0, 0.0, 0.0], _B: [0.0, 1.0, 0.0, 0.0], _C: [0.6, 0.8, 0.0, 0.0]}


def _inputs(per_person=5):
    label_map, rows = {}, []
    fid = 0
    for pk, vec in _VECS.items():
        for _ in range(per_person):
            fid += 1
            label_map[fid] = pk
            rows.append(vec)
    face_ids = np.arange(1, fid + 1, dtype=np.int64)
    X = np.asarray(rows, dtype=np.float32)
    X /= np.linalg.norm(X, axis=1, keepdims=True)
    return label_map, face_ids, X


def test_corrupt_moves_the_requested_share_to_another_listed_person():
    label_map, face_ids, X = _inputs()
    out = corruption.corrupt(label_map, list(label_map), face_ids, X, set(_VECS), 0.4, 7, "random")

    assert len(out) == 6
    assert all(wrong != label_map[f] and wrong in _VECS for f, wrong in out.items())
    assert out == corruption.corrupt(label_map, list(label_map), face_ids, X, set(_VECS), 0.4, 7, "random")


def test_lookalike_picks_the_closest_other_person():
    label_map, face_ids, X = _inputs()
    a_faces = [f for f, pk in label_map.items() if pk == _A]
    out = corruption.corrupt(label_map, a_faces, face_ids, X, set(_VECS), 1.0, 0, "lookalike")

    assert set(out.values()) == {_C}  # C sits between A and B, closer to A than B is


def test_corrupt_never_crosses_spaces_or_targets_unlisted_persons():
    label_map, face_ids, X = _inputs()
    label_map[1] = ("shared", 9)
    out = corruption.corrupt(label_map, [1, 6], face_ids, X, {_A, _B, _C, ("shared", 9)},
                             1.0, 0, "random")

    assert 1 not in out  # its space has no other listed person
    assert out[6] in {_A, _C}


def test_unknown_mode_is_refused():
    label_map, face_ids, X = _inputs()
    with pytest.raises(ValueError):
        corruption.corrupt(label_map, list(label_map), face_ids, X, set(_VECS), 0.5, 0, "nonsense")


def test_photo_tags_follow_the_corrupted_face():
    label_map = {1: _A, 2: _A}
    meta = {1: {"space": "personal", "photo_id": 10}, 2: {"space": "personal", "photo_id": 10}}
    photos = {("personal", 1, 10)}

    alone = corruption._photos_after(photos, {1: _A}, {1: _B}, meta)
    assert alone == {("personal", 2, 10)}
    shared = corruption._photos_after(photos, label_map, {1: _B}, meta)
    assert shared == {("personal", 1, 10), ("personal", 2, 10)}


def test_run_corruption_catches_wrong_tags_on_a_clean_library(db_helpers, tmp_settings):
    h = db_helpers
    syno = 0
    photo = 0
    for pk, vec in [(_A, [1.0, 0.0, 0.0, 0.0]), (_B, [0.0, 1.0, 0.0, 0.0]),
                    (_C, [0.0, 0.0, 1.0, 0.0])]:
        h.insert_person(*pk, f"P{pk[1]}")
        for _ in range(6):
            h.insert_photo("personal", photo)
            fid = h.insert_face("personal", photo, 100, 100, 200, 200)
            h.insert_embedding(fid, "arcface", vec)
            syno += 1
            h.insert_syno_face("personal", syno, photo, pk[1], (0.1, 0.1, 0.3, 0.3))
            h.insert_person_photo("personal", pk[1], photo)
            photo += 1
    h.commit()

    metrics = corruption.run_corruption(h.conn, tmp_settings, fraction=0.2, seed=3)

    assert metrics["baseline_listed_reassigns"] == 0
    for mode in corruption.MODES:
        m = metrics[mode]
        assert m["corrupted"] == 4
        assert m["recall"] == 1.0
        assert m["new_false_alarms"] == 0
        assert m["misdirected"] == 0
