"""Tests for the shared review data layer (review/queries.py)."""

from __future__ import annotations

import json

import pytest

from synopticon.config import load_settings
from synopticon.db import store
from synopticon.review import queries


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "synopticon.db")
    yield c
    c.close()


@pytest.fixture
def settings(tmp_path):
    return load_settings(
        storage={"data_dir": tmp_path},
        nas={"url": "https://nas.test", "account": "svc", "password": "pw"},
    )


def _add_item(conn, kind, payload, confidence=None, status="pending"):
    cur = conn.execute(
        "INSERT INTO review_queue (kind, payload_json, confidence, status, created_at) "
        "VALUES (?,?,?,?,?)",
        (kind, json.dumps(payload), confidence, status, store.now()),
    )
    conn.commit()
    return int(cur.lastrowid)


def test_load_review_items_pagination(conn, settings):
    ids = [
        _add_item(conn, "assign", {"face_id": None, "space": "personal", "photo_id": i})
        for i in range(5)
    ]

    page1 = queries.load_review_items(conn, settings, kind="assign", limit=2, offset=0)
    page2 = queries.load_review_items(conn, settings, kind="assign", limit=2, offset=2)
    page3 = queries.load_review_items(conn, settings, kind="assign", limit=2, offset=4)

    assert [it["item_id"] for it in page1] == ids[:2]
    assert [it["item_id"] for it in page2] == ids[2:4]
    assert [it["item_id"] for it in page3] == ids[4:]
    assert queries.count_review_items(conn, kind="assign") == 5


def test_load_review_items_kind_and_status_filter(conn, settings):
    _add_item(conn, "assign", {"space": "personal"}, status="pending")
    _add_item(conn, "assign", {"space": "personal"}, status="approved")
    _add_item(conn, "merge", {"space": "personal"}, status="pending")

    pending_assign = queries.load_review_items(conn, settings, kind="assign", status="pending")
    assert len(pending_assign) == 1
    assert queries.count_review_items(conn, kind="assign", status="pending") == 1
    # no kind -> all statuses collapse to the status filter
    assert queries.count_review_items(conn, status="pending") == 2


def test_load_review_items_derived_flags(conn, settings):
    # reassign to an unnamed target person
    _add_item(
        conn,
        "reassign",
        {"space": "personal", "photo_id": 1, "person_id": 7, "person_name": ""},
    )
    # merge of two unnamed people
    _add_item(
        conn,
        "merge",
        {"person_a": {"space": "personal", "person_id": 1, "name": ""},
         "person_b": {"space": "personal", "person_id": 2, "name": ""}},
    )
    named = _add_item(
        conn,
        "merge_named",
        {"person_a": {"space": "personal", "person_id": 1, "name": "Alice"},
         "person_b": {"space": "personal", "person_id": 2, "name": "Bob"}},
    )

    by_kind = {
        it["kind"]: it
        for it in queries.load_review_items(conn, settings, status="pending")
    }
    assert by_kind["reassign"]["unnamed_target"] is True
    assert by_kind["merge"]["unnamed_merge"] is True
    assert by_kind["merge_named"]["named_merge"] is True
    assert by_kind["merge_named"]["item_id"] == named


@pytest.mark.parametrize("kind", ["assign", "low_confidence", "reassign"])
def test_unnamed_target_covers_every_targeted_kind(conn, settings, kind):
    unnamed = _add_item(
        conn, kind, {"space": "personal", "photo_id": 1, "person_id": 7, "person_name": ""}
    )
    missing = _add_item(
        conn, kind, {"space": "personal", "photo_id": 2, "person_id": 8}
    )
    onlyspaces = _add_item(
        conn, kind, {"space": "personal", "photo_id": 3, "person_id": 9, "person_name": "  "}
    )
    named = _add_item(
        conn,
        kind,
        {"space": "personal", "photo_id": 4, "person_id": 10, "person_name": "Alice"},
    )

    flags = {
        it["item_id"]: it["unnamed_target"]
        for it in queries.load_review_items(conn, settings, kind=kind)
    }
    assert flags[unnamed] is True
    assert flags[missing] is True
    assert flags[onlyspaces] is True
    assert flags[named] is False


def test_unnamed_target_never_set_for_untargeted_kinds(conn, settings):
    _add_item(conn, "new_person", {"face_ids": [1, 2], "space": "personal"})
    _add_item(
        conn,
        "merge",
        {"person_a": {"space": "personal", "person_id": 1, "name": ""},
         "person_b": {"space": "personal", "person_id": 2, "name": ""}},
    )

    for it in queries.load_review_items(conn, settings, status="pending"):
        assert it["unnamed_target"] is False


def test_crop_url_and_deep_links(conn, settings):
    crops_dir = settings.storage.crops_dir
    crops_dir.mkdir(parents=True, exist_ok=True)
    crop_path = str(crops_dir / "personal" / "1" / "9.jpg")
    conn.execute(
        "INSERT INTO faces (face_id, space, photo_id, detector, x, y, w, h, "
        "crop_path, pipeline_version, created_at) VALUES (9,?,?,?,?,?,?,?,?,?,?)",
        ("personal", 1, "scrfd", 0, 0, 10, 10, crop_path, "v1", store.now()),
    )
    conn.commit()
    _add_item(conn, "assign", {"face_id": 9, "space": "personal", "photo_id": 1})

    (item,) = queries.load_review_items(conn, settings, kind="assign")
    assert item["crop"] == "/crops/personal/1/9.jpg"
    assert item["item_url"].startswith("https://nas.test/?launchApp=")
    assert "personal_space/timeline/item/1" in item["item_url"]


def test_item_url_points_at_similar_group_top_pick(conn, settings):
    """A review item whose photo is a non-top-pick group member must link to the
    group's top pick instead -- the grouped timeline omits the member entirely."""
    now = store.now()
    conn.execute(
        "INSERT INTO photos (id, space, synced_at, similar_top_pick) VALUES (2, 'personal', ?, 1)",
        (now,),
    )
    conn.execute(
        "INSERT INTO photos (id, space, synced_at, similar_top_pick) VALUES (1, 'personal', ?, 1)",
        (now,),
    )
    conn.commit()
    _add_item(conn, "assign", {"face_id": None, "space": "personal", "photo_id": 2})

    (item,) = queries.load_review_items(conn, settings, kind="assign")
    assert "personal_space/timeline/item/1" in item["item_url"]
    assert "timeline/item/2" not in item["item_url"]


def test_hidden_person_flag(conn, settings):
    conn.execute(
        "INSERT INTO persons (id, space, name, show, synced_at) VALUES (?,?,?,?,?)",
        (7, "personal", None, 0, store.now()),
    )
    conn.commit()
    _add_item(conn, "reassign", {"space": "personal", "photo_id": 1, "person_id": 7})

    (item,) = queries.load_review_items(conn, settings, kind="reassign")
    assert item["target_hidden"] is True


def test_decide_item(conn, settings):
    iid = _add_item(conn, "assign", {"space": "personal"})
    assert queries.decide_item(conn, iid, "approve") == "approved"
    row = conn.execute(
        "SELECT status, decided_by FROM review_queue WHERE item_id = ?", (iid,)
    ).fetchone()
    assert row["status"] == "approved"
    assert row["decided_by"] == "review-ui"
    assert queries.decide_item(conn, iid, "bogus") is None


def test_undo_decision_reverts_to_pending(conn, settings):
    for decision in ("approve", "reject"):
        iid = _add_item(conn, "assign", {"space": "personal"})
        queries.decide_item(conn, iid, decision)
        assert queries.undo_decision(conn, iid) == "pending"
        row = conn.execute(
            "SELECT status, decided_at, decided_by FROM review_queue "
            "WHERE item_id = ?",
            (iid,),
        ).fetchone()
        assert row["status"] == "pending"
        assert row["decided_at"] is None
        assert row["decided_by"] is None


def test_undo_decision_refused_on_non_undoable_states(conn, settings):
    # pending (never decided) -> refused, DB untouched
    pending = _add_item(conn, "assign", {"space": "personal"}, status="pending")
    assert queries.undo_decision(conn, pending) is None
    assert (
        conn.execute(
            "SELECT status FROM review_queue WHERE item_id = ?", (pending,)
        ).fetchone()["status"]
        == "pending"
    )
    # applied / failed -> refused, status preserved
    for state in ("applied", "failed"):
        iid = _add_item(conn, "assign", {"space": "personal"}, status=state)
        assert queries.undo_decision(conn, iid) is None
        assert (
            conn.execute(
                "SELECT status FROM review_queue WHERE item_id = ?", (iid,)
            ).fetchone()["status"]
            == state
        )
    # missing item -> refused
    assert queries.undo_decision(conn, 999999) is None


def test_bulk_approve_respects_confidence(conn, settings):
    _add_item(conn, "assign", {"space": "personal"}, confidence=0.9)
    _add_item(conn, "assign", {"space": "personal"}, confidence=0.2)
    _add_item(conn, "assign", {"space": "personal"}, confidence=None)

    n = queries.bulk_approve(conn, "assign", min_confidence=0.5)
    assert n == 1
    approved = conn.execute(
        "SELECT COUNT(*) AS c FROM review_queue WHERE status = 'approved'"
    ).fetchone()["c"]
    assert approved == 1


def test_set_suggested_name(conn, settings):
    np_id = _add_item(conn, "new_person", {"face_ids": [1, 2]})
    other = _add_item(conn, "assign", {"space": "personal"})

    assert queries.set_suggested_name(conn, np_id, "Carol") is True
    payload = json.loads(
        conn.execute(
            "SELECT payload_json FROM review_queue WHERE item_id = ?", (np_id,)
        ).fetchone()["payload_json"]
    )
    assert payload["suggested_name"] == "Carol"

    # wrong kind -> no-op
    assert queries.set_suggested_name(conn, other, "Nope") is False
    assert queries.set_suggested_name(conn, 999999, "Missing") is False


def test_queue_counts(conn, settings):
    _add_item(conn, "assign", {}, status="pending")
    _add_item(conn, "assign", {}, status="pending")
    _add_item(conn, "assign", {}, status="approved")
    _add_item(conn, "merge", {}, status="pending")

    counts = queries.queue_counts(conn)
    assert counts["pending"]["assign"] == 2
    assert counts["pending"]["merge"] == 1
    assert counts["approved"]["assign"] == 1


def test_named_merge_pairs(conn, settings):
    _add_item(
        conn,
        "merge_named",
        {"person_a": {"person_id": 1, "name": "Alice"},
         "person_b": {"person_id": 2, "name": "Bob"}},
        status="approved",
    )
    # not approved -> excluded
    _add_item(
        conn,
        "merge_named",
        {"person_a": {"person_id": 3, "name": "Carol"},
         "person_b": {"person_id": 4, "name": "Dave"}},
        status="pending",
    )
    # unnamed b -> label falls back to person_id
    _add_item(
        conn,
        "merge_named",
        {"person_a": {"person_id": 5, "name": "Eve"},
         "person_b": {"person_id": 6, "name": None}},
        status="approved",
    )

    pairs = queries.named_merge_pairs(conn)
    assert len(pairs) == 2
    assert pairs[0]["label_a"] == "Alice"
    assert pairs[0]["label_b"] == "Bob"
    assert pairs[0]["person_a"]["person_id"] == 1
    assert pairs[1]["label_b"] == 6  # falls back to person_id when name is empty
