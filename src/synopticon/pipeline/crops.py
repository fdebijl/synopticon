"""Crop image maintenance: regenerate from the DB, or wipe to reclaim disk.

Crop images (`<face_id>.jpg` + `<face_id>_ctx.jpg` under `crops_dir`) are a pure
*derived* artifact of extraction — every input needed to rebuild one already
lives on the `faces` row (bbox + landmarks) plus the original on the NAS. So they
can be deleted freely to save space and rebuilt on demand, without re-running
detection or embedding.

`regen_crops` mirrors the runner: it takes a `fetch_original(row) -> Path`
callable (never importing `sync/`) and commits per photo, so an interrupted pass
resumes where it left off. It reuses the exact `align` + `_crop_paths` logic the
runner writes with, so regenerated crops are byte-identical to freshly extracted
ones.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
from collections.abc import Callable
from pathlib import Path

from ..config import Settings, Space
from ..db import store
from . import align
from .runner import _crop_paths, load_image_bgr

log = logging.getLogger(__name__)

Progress = Callable[[int, int | None], None]
FetchOriginal = Callable[[sqlite3.Row], Path]


def _crops_present(crops_dir: Path, face_id: int) -> bool:
    """True when both the aligned and context crop already exist on disk."""
    crop_path, ctx_path = _crop_paths(crops_dir, face_id)
    return crop_path.exists() and ctx_path.exists()


def _photos_with_faces(conn: sqlite3.Connection, space: Space) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT p.* FROM photos p WHERE p.space = ? AND p.deleted = 0 "
        "AND EXISTS (SELECT 1 FROM faces f WHERE f.space = p.space AND f.photo_id = p.id) "
        "ORDER BY p.id",
        (space,),
    ).fetchall()


def regen_crops(
    conn: sqlite3.Connection,
    settings: Settings,
    fetch_original: FetchOriginal,
    space: Space,
    only_missing: bool = True,
    limit: int | None = None,
    progress: Progress | None = None,
) -> dict:
    """Rebuild face crop images from stored bboxes/landmarks + the originals.

    Each photo's original is fetched (via `fetch_original`) and decoded once, then
    every one of its faces' crops is re-emitted. With `only_missing` (the default)
    a photo whose crops are all already on disk is skipped without fetching it, so
    a re-run after a partial wipe only pulls what it must. A photo whose original
    can't be fetched or decoded is skipped and counted — never fatal.
    """
    import cv2

    crops_dir = settings.storage.crops_dir
    crops_dir.mkdir(parents=True, exist_ok=True)

    photos = _photos_with_faces(conn, space)
    total = len(photos)
    processed = failed = crops_written = skipped = 0

    for i, row in enumerate(photos, start=1):
        if limit is not None and processed >= limit:
            break
        pid = int(row["id"])
        faces = conn.execute(
            "SELECT face_id, x, y, w, h, landmarks FROM faces "
            "WHERE space = ? AND photo_id = ?",
            (space, pid),
        ).fetchall()

        todo = faces
        if only_missing:
            todo = [f for f in faces if not _crops_present(crops_dir, int(f["face_id"]))]
            skipped += len(faces) - len(todo)
        if not todo:
            if progress and (i % 25 == 0 or i == total):
                progress(i, total)
            continue

        try:
            img_bgr = load_image_bgr(fetch_original(row))
        except Exception as exc:  # noqa: BLE001 - one unavailable original must not abort
            log.warning("regen: skipping photo %s (space=%s): %s", pid, space, exc)
            failed += 1
            if progress and (i % 25 == 0 or i == total):
                progress(i, total)
            continue

        for f in todo:
            face_id = int(f["face_id"])
            bbox = (float(f["x"]), float(f["y"]), float(f["w"]), float(f["h"]))
            lm_blob = f["landmarks"]
            if lm_blob is not None:
                crop = align.norm_crop(img_bgr, store.blob_to_vec(lm_blob).reshape(5, 2))
            else:
                crop = align.resize_crop(img_bgr, bbox)
            ctx = align.context_crop(img_bgr, bbox)
            crop_path, ctx_path = _crop_paths(crops_dir, face_id)
            cv2.imwrite(str(crop_path), crop)
            cv2.imwrite(str(ctx_path), ctx)
            conn.execute(
                "UPDATE faces SET crop_path = ?, ctx_crop_path = ? WHERE face_id = ?",
                (str(crop_path), str(ctx_path), face_id),
            )
            crops_written += 1

        conn.commit()
        processed += 1
        if progress and (i % 25 == 0 or i == total):
            progress(i, total)

    return {
        "photos": processed,
        "crops": crops_written,
        "skipped": skipped,
        "failed": failed,
    }


def crops_disk_usage(crops_dir: Path) -> tuple[int, int]:
    """Return (file count, total bytes) of the crop images currently on disk.

    Uses ``os.scandir`` rather than ``Path.rglob``: a populated library holds
    >100k crops, and rglob builds a ``Path`` per entry then pays a *second*
    stat for ``is_file()`` on top of the one for the size. ``DirEntry`` answers
    both from the cached dirent.
    """
    files = 0
    nbytes = 0
    stack = [str(crops_dir)]
    while stack:
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        files += 1
                        nbytes += entry.stat(follow_symlinks=False).st_size
        except OSError:  # vanished or unreadable mid-walk; the figure is advisory
            continue
    return files, nbytes


def delete_crops(crops_dir: Path) -> None:
    """Remove every crop image, leaving an empty crops dir. The DB is untouched;
    `regen_crops` rebuilds from it later."""
    shutil.rmtree(crops_dir, ignore_errors=True)
    crops_dir.mkdir(parents=True, exist_ok=True)
