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


def test_reassign_detected_for_mislabeled_face():
    settings = load_settings()
    # One cluster: 4 labeled X, 1 labeled Y (Synology got it wrong).
    specs = [(i, [1, 0, 0, 0]) for i in range(1, 6)]
    label_map = {i: ("personal", 100) for i in range(1, 5)}
    label_map[5] = ("personal", 200)
    face_ids, X, labels, lm, meta = _core_inputs(specs, [0] * 5, label_map)
    result = crossref._crossref_core(
        face_ids, X, labels, lm, settings, meta, person_photos=set(),
        syno_face_ids={5: 555},
    )
    assert len(result.reassigns) == 1
    item = result.reassigns[0]
    assert item["person_key"] == ("personal", 100)
    assert item["from_person_key"] == ("personal", 200)
    p = item["payload"]
    assert p["face_id"] == 5
    assert p["syno_face_id"] == 555
    assert p["from_person_id"] == 200
    assert p["person_id"] == 100
    assert p["confidence"] > 0.99
    # Existing semantics untouched: the disputed face still predicts X, and
    # no assigns appear (every face is labeled).
    assert result.predicted_person[5] == ("personal", 100)
    assert result.assigns == []


def test_reassign_requires_loo_support():
    settings = load_settings()
    # 2 X + 1 Y: full mapping holds (2/3 >= 0.6, labeled_total 3 >= min_labeled 3)
    # but excluding the disputed vote leaves 2 < min_labeled -> no reassign.
    specs = [(i, [1, 0, 0, 0]) for i in range(1, 4)]
    label_map = {1: ("personal", 100), 2: ("personal", 100), 3: ("personal", 200)}
    face_ids, X, labels, lm, meta = _core_inputs(specs, [0] * 3, label_map)
    result = crossref._crossref_core(
        face_ids, X, labels, lm, settings, meta, person_photos=set(),
        syno_face_ids={3: 555},
    )
    assert result.clusters[0].mapped_person == ("personal", 100)
    assert result.reassigns == []


def test_reassign_skips_photo_fallback_labels():
    settings = load_settings()
    # Same as the detection case, but the mislabeled face has no syno_faces
    # row (photo-level fallback label) -> nothing to separate on the NAS.
    specs = [(i, [1, 0, 0, 0]) for i in range(1, 6)]
    label_map = {i: ("personal", 100) for i in range(1, 5)}
    label_map[5] = ("personal", 200)
    face_ids, X, labels, lm, meta = _core_inputs(specs, [0] * 5, label_map)
    result = crossref._crossref_core(
        face_ids, X, labels, lm, settings, meta, person_photos=set(),
        syno_face_ids={},
    )
    assert result.reassigns == []


def test_reassign_below_assign_sim_not_emitted():
    settings = load_settings()
    # The Y-labeled face sits in the cluster but is dissimilar to the X
    # members (cos ~0.4 < assign_sim 0.55) -> too weak to contradict Synology.
    specs = [(i, [1, 0, 0, 0]) for i in range(1, 5)]
    specs.append((5, [0.4, 0.9, 0, 0]))
    label_map = {i: ("personal", 100) for i in range(1, 5)}
    label_map[5] = ("personal", 200)
    face_ids, X, labels, lm, meta = _core_inputs(specs, [0] * 5, label_map)
    result = crossref._crossref_core(
        face_ids, X, labels, lm, settings, meta, person_photos=set(),
        syno_face_ids={5: 555},
    )
    assert result.reassigns == []


def test_reassign_skips_cross_space_moves():
    settings = load_settings()
    specs = [(i, [1, 0, 0, 0]) for i in range(1, 6)]
    label_map = {i: ("personal", 100) for i in range(1, 5)}
    label_map[5] = ("shared", 200)  # Synology label lives in the other space
    face_ids, X, labels, lm, meta = _core_inputs(specs, [0] * 5, label_map)
    result = crossref._crossref_core(
        face_ids, X, labels, lm, settings, meta, person_photos=set(),
        syno_face_ids={5: 555},
    )
    assert result.reassigns == []


def test_core_backward_compatible_without_syno_ids():
    settings = load_settings()
    specs = [(i, [1, 0, 0, 0]) for i in range(1, 6)]
    label_map = {i: ("personal", 100) for i in range(1, 5)}
    label_map[5] = ("personal", 200)
    face_ids, X, labels, lm, meta = _core_inputs(specs, [0] * 5, label_map)
    # Positional call exactly as eval/holdout does it -> no reassign pass.
    result = crossref._crossref_core(face_ids, X, labels, lm, settings, meta, set())
    assert result.reassigns == []


def test_reassign_from_similarity_evidence():
    settings = load_settings()
    # Cluster 0: 4 X + 1 mislabeled Y. Cluster 1: 2 genuine Y faces far away.
    specs = [(i, [1, 0, 0, 0]) for i in range(1, 6)]
    specs += [(6, [0, 1, 0, 0]), (7, [0, 1, 0, 0])]
    label_map = {i: ("personal", 100) for i in range(1, 5)}
    label_map[5] = ("personal", 200)
    label_map[6] = ("personal", 200)
    label_map[7] = ("personal", 200)
    face_ids, X, labels, lm, meta = _core_inputs(specs, [0] * 5 + [1] * 2, label_map)
    result = crossref._crossref_core(
        face_ids, X, labels, lm, settings, meta, person_photos=set(),
        syno_face_ids={5: 555},
    )
    assert len(result.reassigns) == 1
    from_sim = result.reassigns[0]["payload"]["from_similarity"]
    assert from_sim is not None and from_sim < 0.1  # orthogonal to the real Ys

    # Without any other Y-labeled face, the evidence is None.
    label_map2 = {i: ("personal", 100) for i in range(1, 5)}
    label_map2[5] = ("personal", 200)
    face_ids2, X2, labels2, lm2, meta2 = _core_inputs(specs[:5], [0] * 5, label_map2)
    result2 = crossref._crossref_core(
        face_ids2, X2, labels2, lm2, settings, meta2, person_photos=set(),
        syno_face_ids={5: 555},
    )
    assert len(result2.reassigns) == 1
    assert result2.reassigns[0]["payload"]["from_similarity"] is None


def test_run_clustering_reassign_row_and_dedup(db_helpers, tmp_settings):
    h = db_helpers
    h.insert_person("personal", 100, "Alice")
    h.insert_person("personal", 200, "Bob")
    # 5 identical faces in distinct photos; Synology labeled 4 as Alice and
    # one (photo 4) wrongly as Bob.
    for i in range(5):
        h.insert_photo("personal", i)
        h.insert_face("personal", i, 100, 100, 200, 200)  # -> [0.1,0.1,0.3,0.3]
    for fid in range(1, 6):
        h.insert_embedding(fid, "arcface", [1.0, 0.0, 0.0, 0.0])
    for photo in range(4):
        h.insert_syno_face("personal", photo + 1, photo, 100, (0.1, 0.1, 0.3, 0.3))
    h.insert_syno_face("personal", 5, 4, 200, (0.1, 0.1, 0.3, 0.3))
    h.commit()

    crossref.run_clustering(h.conn, tmp_settings)
    crossref.run_clustering(h.conn, tmp_settings)  # second run must not duplicate

    import json

    rows = h.conn.execute(
        "SELECT payload_json FROM review_queue WHERE kind = 'reassign'"
    ).fetchall()
    assert len(rows) == 1
    p = json.loads(rows[0]["payload_json"])
    assert p["syno_face_id"] == 5
    assert p["from_person_id"] == 200
    assert p["from_person_name"] == "Bob"
    assert p["person_id"] == 100
    assert p["person_name"] == "Alice"


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


def test_hidden_new_person_is_not_reproposed(db_helpers, tmp_settings):
    """Hiding is the sticky counterpart to rejecting: a hidden cluster must not
    come back on the next run, where a rejected one deliberately does."""
    h = db_helpers
    for i in range(8):  # crossref.new_person_min_photos default
        h.insert_photo("personal", i)
        h.insert_face("personal", i, 100, 100, 200, 200)
    for fid in range(1, 9):
        h.insert_embedding(fid, "arcface", [1.0, 0.0, 0.0, 0.0])
    h.commit()

    crossref.run_clustering(h.conn, tmp_settings)
    rows = h.conn.execute(
        "SELECT item_id FROM review_queue WHERE kind = 'new_person'"
    ).fetchall()
    assert len(rows) == 1

    h.conn.execute(
        "UPDATE review_queue SET status = 'hidden' WHERE item_id = ?",
        (rows[0]["item_id"],),
    )
    h.conn.commit()
    crossref.run_clustering(h.conn, tmp_settings)
    assert (
        h.conn.execute(
            "SELECT COUNT(*) AS c FROM review_queue WHERE kind = 'new_person'"
        ).fetchone()["c"]
        == 1
    )

    # A rejected one, by contrast, is offered again.
    h.conn.execute("UPDATE review_queue SET status = 'rejected'")
    h.conn.commit()
    crossref.run_clustering(h.conn, tmp_settings)
    assert (
        h.conn.execute(
            "SELECT COUNT(*) AS c FROM review_queue WHERE kind = 'new_person'"
        ).fetchone()["c"]
        == 2
    )


def test_new_person_needs_photos_not_just_faces(db_helpers, tmp_settings):
    """Enough faces, too few photos: a burst of one moment is not a person."""
    h = db_helpers
    for photo in range(2):
        h.insert_photo("personal", photo)
        for n in range(5):
            h.insert_face("personal", photo, 100 * n, 100, 80, 80)
    for fid in range(1, 11):
        h.insert_embedding(fid, "arcface", [1.0, 0.0, 0.0, 0.0])
    h.commit()

    crossref.run_clustering(h.conn, tmp_settings)
    assert (
        h.conn.execute(
            "SELECT COUNT(*) AS c FROM review_queue WHERE kind = 'new_person'"
        ).fetchone()["c"]
        == 0
    )

    tmp_settings.crossref.new_person_min_photos = 2
    crossref.run_clustering(h.conn, tmp_settings)
    row = h.conn.execute(
        "SELECT payload_json FROM review_queue WHERE kind = 'new_person'"
    ).fetchone()
    assert row is not None
    import json

    payload = json.loads(row["payload_json"])
    assert payload["size"] == 10
    assert payload["photo_count"] == 2


def test_retargeted_assign_does_not_resurrect_the_old_suggestion(
    db_helpers, tmp_settings
):
    import json

    h = db_helpers
    h.insert_person("personal", 100, "Alice")
    h.insert_person("personal", 200, "Bob")
    for i in range(4):
        h.insert_photo("personal", i)
        h.insert_face("personal", i, 100, 100, 200, 200)
    for fid in range(1, 5):
        h.insert_embedding(fid, "arcface", [1.0, 0.0, 0.0, 0.0])
    for si, photo in enumerate([0, 1, 2]):
        h.insert_syno_face("personal", si + 1, photo, 100, (0.1, 0.1, 0.3, 0.3))
    h.commit()

    crossref.run_clustering(h.conn, tmp_settings)
    row = h.conn.execute(
        "SELECT item_id, payload_json FROM review_queue WHERE kind = 'assign'"
    ).fetchone()
    payload = json.loads(row["payload_json"])
    assert (payload["face_id"], payload["person_id"]) == (4, 100)

    # A human says "no, that's Bob" — the (4, 100) identity now exists nowhere.
    from synopticon.review import queries

    queries.retarget_item(h.conn, row["item_id"], "personal", 200, "Bob")

    crossref.run_clustering(h.conn, tmp_settings)
    identities = [
        (json.loads(r["payload_json"])["face_id"], json.loads(r["payload_json"])["person_id"])
        for r in h.conn.execute(
            "SELECT payload_json FROM review_queue WHERE kind IN ('assign','low_confidence')"
        )
    ]
    assert identities == [(4, 200)]


def _split_person_cluster(h, name_a, name_b):
    """5 identical faces in one cluster: 3 labeled person 10, 2 labeled person 20."""
    h.insert_person("personal", 10, name_a)
    h.insert_person("personal", 20, name_b)
    for i in range(5):
        h.insert_photo("personal", i)
        h.insert_face("personal", i, 100, 100, 200, 200)  # -> [0.1,0.1,0.3,0.3]
    for fid in range(1, 6):
        h.insert_embedding(fid, "arcface", [1.0, 0.0, 0.0, 0.0])
    for photo in range(3):  # 3 faces -> person 10 (majority)
        h.insert_syno_face("personal", photo + 1, photo, 10, (0.1, 0.1, 0.3, 0.3))
    for photo in (3, 4):  # 2 faces -> person 20
        h.insert_syno_face("personal", photo + 1, photo, 20, (0.1, 0.1, 0.3, 0.3))
    h.commit()


def test_run_clustering_named_merge_gets_distinct_kind(db_helpers, tmp_settings):
    # Both split persons are named -> the merge is the dangerous 'merge_named'.
    h = db_helpers
    _split_person_cluster(h, "Alice", "Bob")
    crossref.run_clustering(h.conn, tmp_settings)

    rows = h.conn.execute(
        "SELECT kind FROM review_queue WHERE kind LIKE 'merge%'"
    ).fetchall()
    assert [r["kind"] for r in rows] == ["merge_named"]


def test_run_clustering_unnamed_side_stays_plain_merge(db_helpers, tmp_settings):
    # One side unnamed -> ordinary 'merge', not 'merge_named'.
    h = db_helpers
    _split_person_cluster(h, "Alice", None)
    crossref.run_clustering(h.conn, tmp_settings)

    rows = h.conn.execute(
        "SELECT kind FROM review_queue WHERE kind LIKE 'merge%'"
    ).fetchall()
    assert [r["kind"] for r in rows] == ["merge"]


def test_migration_0005_reclassifies_pending_named_merges(tmp_path):
    import json
    from pathlib import Path

    from synopticon.db import store

    conn = store.connect(tmp_path / "db.sqlite")
    named = json.dumps({"person_a": {"name": "A"}, "person_b": {"name": "B"}})
    half = json.dumps({"person_a": {"name": "A"}, "person_b": {"name": ""}})
    for status in ("pending", "approved", "applied", "rejected"):
        conn.execute(
            "INSERT INTO review_queue (kind, payload_json, status, created_at) "
            "VALUES ('merge', ?, ?, 0)",
            (named, status),
        )
    conn.execute(
        "INSERT INTO review_queue (kind, payload_json, status, created_at) "
        "VALUES ('merge', ?, 'pending', 0)",
        (half,),
    )
    conn.commit()

    sql = (
        Path(store.__file__).parent / "migrations" / "0005_split_merge_named.sql"
    ).read_text()
    conn.executescript(sql)

    reclassified = {
        r["status"]
        for r in conn.execute("SELECT status FROM review_queue WHERE kind = 'merge_named'")
    }
    # Only un-applied both-named rows move; historical + half-named stay 'merge'.
    assert reclassified == {"pending", "approved"}
    assert conn.execute(
        "SELECT COUNT(*) c FROM review_queue WHERE kind = 'merge'"
    ).fetchone()["c"] == 3
