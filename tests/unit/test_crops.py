"""Crop maintenance tests (pipeline/crops.py) — no NAS, no models.

`regen_crops` takes its originals through an injected `fetch_original`, so a real
image on disk written by the test is the whole fixture. Every case here is about
which of the three per-face outcomes a face lands in: redrawn from the original,
backfilled in the DB alone, or skipped.
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from synopticon.config import load_settings
from synopticon.db import store
from synopticon.pipeline import crops as crops_mod
from synopticon.pipeline.crops import delete_crops, regen_crops
from synopticon.pipeline.runner import _crop_paths


@pytest.fixture
def settings(tmp_path):
    return load_settings(
        storage={"data_dir": tmp_path},
        nas={"url": "https://nas.test", "account": "svc", "password": "pw"},
    )


@pytest.fixture
def conn(settings):
    c = store.connect(settings.storage.db_path)
    yield c
    c.close()


@pytest.fixture
def original(tmp_path):
    """A real decodable JPEG the fake `fetch_original` hands back."""
    path = tmp_path / "original.jpg"
    img = np.full((400, 400, 3), 128, dtype=np.uint8)
    cv2.imwrite(str(path), img)
    return path


def _seed(conn, face_ids=(1, 2), photo_id=1, deleted=0):
    conn.execute(
        "INSERT INTO photos (id, space, filename, width, height, synced_at, deleted) "
        "VALUES (?,'personal','p.jpg',400,400,?,?)",
        (photo_id, store.now(), deleted),
    )
    for n, fid in enumerate(face_ids):
        # faces is UNIQUE on (space, photo_id, detector, bbox), so vary the bbox.
        conn.execute(
            "INSERT INTO faces (face_id, space, photo_id, detector, x, y, w, h, "
            "pipeline_version, created_at) "
            "VALUES (?,'personal',?, 'scrfd', ?, ?, 100, 100, 'v1', ?)",
            (fid, photo_id, 20 + n * 120, 20 + n * 120, store.now()),
        )
    conn.commit()


def _paths_of(conn, face_id):
    row = conn.execute(
        "SELECT crop_path, ctx_crop_path FROM faces WHERE face_id = ?", (face_id,)
    ).fetchone()
    return row["crop_path"], row["ctx_crop_path"]


def test_regen_writes_crops_and_records_the_paths(conn, settings, original):
    _seed(conn)
    stats = regen_crops(conn, settings, lambda row: original, space="personal")

    assert stats["photos"] == 1
    assert stats["crops"] == 2
    assert stats["backfilled"] == 0
    for fid in (1, 2):
        crop_path, ctx_path = _paths_of(conn, fid)
        assert crop_path and ctx_path
        expected, expected_ctx = _crop_paths(settings.storage.crops_dir, fid)
        assert crop_path == str(expected)
        assert ctx_path == str(expected_ctx)
        assert expected.exists() and expected_ctx.exists()


def test_regen_backfills_a_null_crop_path_without_fetching(conn, settings, original):
    """The bug this fixes: files on disk + NULL columns used to be skipped forever.

    The review UI reads `faces.crop_path`, never the disk, so those faces showed no
    crop while the disk check declared the photo done. Repairing them is a bare
    UPDATE — asserted here by making `fetch_original` fail the test if called.
    """
    _seed(conn)
    regen_crops(conn, settings, lambda row: original, space="personal")
    conn.execute("UPDATE faces SET crop_path = NULL, ctx_crop_path = NULL")
    conn.commit()

    def no_fetch(row):
        raise AssertionError("a backfill must not fetch the original")

    stats = regen_crops(conn, settings, no_fetch, space="personal")

    assert stats["backfilled"] == 2
    assert stats["crops"] == 0
    assert stats["failed"] == 0
    for fid in (1, 2):
        crop_path, ctx_path = _paths_of(conn, fid)
        assert crop_path and ctx_path


def test_regen_backfills_a_half_written_row(conn, settings, original):
    """Both columns are written together, so one being NULL means the pair is stale."""
    _seed(conn, face_ids=(1,))
    regen_crops(conn, settings, lambda row: original, space="personal")
    conn.execute("UPDATE faces SET ctx_crop_path = NULL WHERE face_id = 1")
    conn.commit()

    stats = regen_crops(conn, settings, lambda row: original, space="personal")
    assert stats["backfilled"] == 1
    assert all(_paths_of(conn, 1))


def test_regen_backfills_alongside_a_redraw(conn, settings, original):
    """One photo, one face needing pixels and one needing only its columns."""
    _seed(conn, face_ids=(1, 2))
    regen_crops(conn, settings, lambda row: original, space="personal")
    # face 1 loses its images, face 2 loses only its DB columns.
    for path in _crop_paths(settings.storage.crops_dir, 1):
        path.unlink()
    conn.execute("UPDATE faces SET crop_path = NULL, ctx_crop_path = NULL WHERE face_id = 2")
    conn.commit()

    stats = regen_crops(conn, settings, lambda row: original, space="personal")
    assert stats["crops"] == 1
    assert stats["backfilled"] == 1
    assert all(_paths_of(conn, 1))
    assert all(_paths_of(conn, 2))


def test_regen_skips_a_face_that_is_already_whole(conn, settings, original):
    _seed(conn)
    regen_crops(conn, settings, lambda row: original, space="personal")

    def no_fetch(row):
        raise AssertionError("a complete photo must not be fetched again")

    stats = regen_crops(conn, settings, no_fetch, space="personal")
    assert stats == {"photos": 0, "crops": 0, "backfilled": 0, "skipped": 2, "failed": 0}


def test_regen_all_redraws_everything(conn, settings, original):
    _seed(conn)
    regen_crops(conn, settings, lambda row: original, space="personal")
    stats = regen_crops(
        conn, settings, lambda row: original, space="personal", only_missing=False
    )
    assert stats["crops"] == 2
    assert stats["skipped"] == 0
    assert stats["backfilled"] == 0


def test_regen_cannot_reach_a_deleted_photo(conn, settings, original):
    """No original means no rebuild; these faces are prune-queue's problem, not regen's."""
    _seed(conn, deleted=1)
    stats = regen_crops(conn, settings, lambda row: original, space="personal")
    assert stats == {"photos": 0, "crops": 0, "backfilled": 0, "skipped": 0, "failed": 0}
    assert _paths_of(conn, 1) == (None, None)


def test_regen_counts_an_unfetchable_original_as_failed(conn, settings):
    _seed(conn)

    def boom(row):
        raise OSError("NAS unreachable")

    stats = regen_crops(conn, settings, boom, space="personal")
    assert stats["failed"] == 1
    assert stats["crops"] == 0


def test_delete_crops_leaves_the_db_alone(conn, settings, original):
    _seed(conn)
    regen_crops(conn, settings, lambda row: original, space="personal")
    files, nbytes = crops_mod.crops_disk_usage(settings.storage.crops_dir)
    assert files == 4 and nbytes > 0

    delete_crops(settings.storage.crops_dir)
    assert crops_mod.crops_disk_usage(settings.storage.crops_dir) == (0, 0)
    # The columns still point at the wiped files; regen re-writes them.
    assert all(_paths_of(conn, 1))
    stats = regen_crops(conn, settings, lambda row: original, space="personal")
    assert stats["crops"] == 2
