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

The crop is *two* artifacts, not one: the images on disk and the `faces` row's
`crop_path`/`ctx_crop_path` pointing at them. Only the columns are read by
anything downstream — the review UI maps `crop_path` to a `/crops/...` URL and
never looks at the filesystem — so a face whose files exist while its columns sit
NULL is invisible in review. `regen_crops` repairs that pairing without fetching
anything, which is the cheap half of what it does.
"""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Callable
from pathlib import Path

from ..db import Connection, Row

from ..config import Settings, Space
from ..db import store

log = logging.getLogger(__name__)

# `align` and `.runner` are imported lazily by the two functions that actually
# regenerate images. Both pull cv2/numpy at module scope, and the web process
# imports this module solely for `crops_disk_usage` — a directory walk. Keeping
# the heavy imports out of module scope means /api/maintenance/counts cannot pay
# an image-stack import inside a request handler.

Progress = Callable[[int, int | None], None]
FetchOriginal = Callable[[Row], Path]


def _crops_present(crops_dir: Path, face_id: int) -> bool:
    """True when both the aligned and context crop already exist on disk."""
    from .runner import _crop_paths

    crop_path, ctx_path = _crop_paths(crops_dir, face_id)
    return crop_path.exists() and ctx_path.exists()


def _needs_backfill(row: Row) -> bool:
    """True when the crop files are on disk but the ``faces`` row forgot them.

    The review UI reads ``faces.crop_path``, never the disk, so a row whose files
    exist while its columns sit NULL renders with no crop at all — and the disk
    check in :func:`_crops_present` used to declare that photo done and skip it,
    which is why a ``regen-crops`` pass could not repair it. Both columns are
    written together by the runner, so either being unset means the pair is stale.
    """
    return not row["crop_path"] or not row["ctx_crop_path"]


def _backfill_paths(conn: Connection, crops_dir: Path, faces: list[Row]) -> None:
    """Point ``crop_path``/``ctx_crop_path`` at the images already on disk.

    No image is decoded and no original is fetched: the paths are a pure function
    of ``face_id``, so re-deriving them is the whole repair. The caller commits.
    """
    from .runner import _crop_paths

    for f in faces:
        face_id = int(f["face_id"])
        crop_path, ctx_path = _crop_paths(crops_dir, face_id)
        conn.execute(
            "UPDATE faces SET crop_path = ?, ctx_crop_path = ? WHERE face_id = ?",
            (str(crop_path), str(ctx_path), face_id),
        )


def _photos_with_faces(conn: Connection, space: Space) -> list[Row]:
    return conn.execute(
        "SELECT p.* FROM photos p WHERE p.space = ? AND p.deleted = 0 "
        "AND EXISTS (SELECT 1 FROM faces f WHERE f.space = p.space AND f.photo_id = p.id) "
        "ORDER BY p.id",
        (space,),
    ).fetchall()


def regen_crops(
    conn: Connection,
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
    a photo whose crops are all already on disk *and* recorded on its `faces` row is
    skipped without fetching it, so a re-run after a partial wipe only pulls what it
    must; a face whose files are on disk but whose columns are unset is repaired with
    a bare UPDATE (counted as `backfilled`), no fetch involved. `only_missing=False`
    redraws everything unconditionally. A photo whose original can't be fetched or
    decoded is skipped and counted — never fatal.

    Faces on a photo that is deleted or absent from `photos` are out of reach here by
    construction (`_photos_with_faces` filters them), because there is no original to
    rebuild from. Review items pointing only at those are what `prune-queue` clears.
    """
    import cv2

    from . import align
    from .runner import _crop_paths, load_image_bgr, skip_reason

    crops_dir = settings.storage.crops_dir
    crops_dir.mkdir(parents=True, exist_ok=True)

    photos = _photos_with_faces(conn, space)
    total = len(photos)
    processed = failed = crops_written = skipped = backfilled = 0

    for i, row in enumerate(photos, start=1):
        if limit is not None and processed >= limit:
            break
        pid = int(row["id"])
        faces = conn.execute(
            "SELECT face_id, x, y, w, h, landmarks, crop_path, ctx_crop_path FROM faces "
            "WHERE space = ? AND photo_id = ?",
            (space, pid),
        ).fetchall()

        # Three outcomes per face: redraw it from the original, backfill just the
        # DB columns because the images are already on disk, or leave it alone.
        todo: list[Row] = []
        backfill: list[Row] = []
        for f in faces:
            if not only_missing:
                todo.append(f)
            elif not _crops_present(crops_dir, int(f["face_id"])):
                todo.append(f)
            elif _needs_backfill(f):
                backfill.append(f)
            else:
                skipped += 1

        if backfill and not todo:
            # Pure DB repair: no original to fetch, no image work, no NAS traffic.
            _backfill_paths(conn, crops_dir, backfill)
            conn.commit()
            backfilled += len(backfill)
            processed += 1
            if progress and (i % 25 == 0 or i == total):
                progress(i, total)
            continue
        if not todo:
            if progress and (i % 25 == 0 or i == total):
                progress(i, total)
            continue

        try:
            img_bgr = load_image_bgr(fetch_original(row))
        except Exception as exc:  # noqa: BLE001 - one unavailable original must not abort
            filename = row["filename"] if "filename" in row.keys() else None
            log.warning(
                "regen: skipping photo %s (%s, space=%s): %s [%s: %s]",
                pid, filename or "unknown file", space,
                skip_reason(exc, filename), type(exc).__name__, exc,
            )
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

        if backfill:
            _backfill_paths(conn, crops_dir, backfill)
            backfilled += len(backfill)

        conn.commit()
        processed += 1
        if progress and (i % 25 == 0 or i == total):
            progress(i, total)

    return {
        "photos": processed,
        "crops": crops_written,
        "backfilled": backfilled,
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
