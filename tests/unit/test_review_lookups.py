"""Tests for the fingerprint-keyed review lookup cache.

The cache is what keeps ``/api/review/items`` O(page) instead of O(library), so
what matters here is not only that it caches, but that it caches the *right*
things: a review decision must not throw it away, and a pipeline/sync write
must.
"""

from __future__ import annotations

from synopticon.config import load_settings
from synopticon.db import store
from synopticon.review import queries
from synopticon.review.lookups import LookupCache, fingerprint


def _settings(tmp_path):
    return load_settings(storage={"data_dir": tmp_path})


def _seed(conn, crops_dir):
    conn.execute(
        "INSERT INTO photos (id, space, width, height, synced_at) "
        "VALUES (1, 'personal', 100, 100, 0)"
    )
    conn.execute(
        "INSERT INTO persons (id, space, name, show, deleted, synced_at) "
        "VALUES (7, 'personal', 'Ada', 1, 0, 0)"
    )
    _add_face(conn, 1, str(crops_dir / "aa" / "1.jpg"), x=10)
    conn.execute(
        "INSERT INTO review_queue (kind, payload_json, confidence, status, "
        "created_at) VALUES ('assign', ?, 0.9, 'pending', 0)",
        (
            '{"space": "personal", "face_id": 1, "photo_id": 1, '
            '"person_id": 7, "person_name": "Ada"}',
        ),
    )
    conn.commit()


def _add_face(conn, face_id: int, crop_path: str, x: float) -> None:
    conn.execute(
        "INSERT INTO faces (face_id, space, photo_id, detector, x, y, w, h, "
        "det_score, quality, crop_path, pipeline_version, created_at) "
        "VALUES (?, 'personal', 1, 'merged', ?, 10, 20, 20, 0.9, 1.0, ?, 'v1', 0)",
        (face_id, x, crop_path),
    )


def test_cache_survives_a_review_decision_but_not_a_face_write(tmp_path):
    settings = _settings(tmp_path)
    conn = store.connect(settings.storage.db_path)
    _seed(conn, tmp_path / "crops")
    cache = LookupCache()

    first = cache.get(conn, settings)
    assert cache.get(conn, settings) is first  # same object: served from cache

    # Deciding a review item writes to review_queue only. The queue is not an
    # input to any of the three lookups, so the cache must be kept — otherwise
    # every keystroke in the review UI triggers a full-library rebuild.
    queries.decide_item(conn, 1, "approve")
    assert cache.get(conn, settings) is first

    # A new face (what `extract` does) changes the inputs and must invalidate.
    _add_face(conn, 2, str(tmp_path / "crops" / "bb" / "2.jpg"), x=50)
    conn.commit()
    rebuilt = cache.get(conn, settings)
    assert rebuilt is not first
    assert set(rebuilt.crops) == {1, 2}
    conn.close()


def test_fingerprint_tracks_in_place_person_and_label_changes(tmp_path):
    settings = _settings(tmp_path)
    conn = store.connect(settings.storage.db_path)
    _seed(conn, tmp_path / "crops")

    before = fingerprint(conn)
    # Hiding a person changes `hidden_persons` without changing any row count.
    conn.execute("UPDATE persons SET show = 0 WHERE id = 7")
    conn.commit()
    assert fingerprint(conn) != before

    conn.execute(
        "INSERT INTO syno_faces (syno_face_id, space, photo_id, person_id, "
        "x1, y1, x2, y2, synced_at) "
        "VALUES (100, 'personal', 1, 7, 0.1, 0.1, 0.3, 0.3, 0)"
    )
    conn.commit()
    with_face = fingerprint(conn)
    # Relabelling an existing syno face is an in-place person_id change.
    conn.execute("UPDATE syno_faces SET person_id = 8 WHERE syno_face_id = 100")
    conn.commit()
    assert fingerprint(conn) != with_face
    conn.close()


def test_invalidate_forces_a_rebuild(tmp_path):
    settings = _settings(tmp_path)
    conn = store.connect(settings.storage.db_path)
    _seed(conn, tmp_path / "crops")
    cache = LookupCache()

    first = cache.get(conn, settings)
    cache.invalidate()
    assert cache.get(conn, settings) is not first
    conn.close()


def test_crop_url_mapper_matches_crop_url(tmp_path):
    """The fast mapper must be a drop-in for the resolve()-per-call version."""
    crops_dir = tmp_path / "crops"
    (crops_dir / "aa").mkdir(parents=True)
    to_url = queries.crop_url_mapper(crops_dir)
    for candidate in (
        str(crops_dir / "aa" / "1.jpg"),
        str(crops_dir / "aa" / ".." / "aa" / "1.jpg"),
        str(tmp_path / "elsewhere" / "1.jpg"),  # outside the served root
        None,
        "",
    ):
        assert to_url(candidate) == queries.crop_url(candidate, crops_dir)
    assert to_url(str(crops_dir / "aa" / "1.jpg")) == "/crops/aa/1.jpg"


def test_load_review_items_is_identical_with_and_without_the_cache(tmp_path):
    settings = _settings(tmp_path)
    conn = store.connect(settings.storage.db_path)
    _seed(conn, tmp_path / "crops")
    lk = LookupCache().get(conn, settings)

    uncached = queries.load_review_items(conn, settings)
    cached = queries.load_review_items(
        conn,
        settings,
        crops=lk.crops,
        hidden=lk.hidden,
        person_face_map=lk.person_face_map,
    )
    assert cached == uncached
    conn.close()
