"""Content-hash pass for `sync --hash`: sha256 + perceptual hash per photo.

Like the extract pipeline, this takes a `fetch_original(row) -> Path` callable
instead of importing the download layer directly, and commits per photo so an
interrupted pass resumes where it left off. Photos are skipped when their
recorded `hash_cache_key` still matches the NAS `cache_key`, so re-runs only
touch new or edited photos.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from pathlib import Path

import numpy as np
import scipy.fft

from synopticon.config import Space
from synopticon.db import Connection, Row, store

log = logging.getLogger(__name__)

_PHASH_SIZE = 8          # 8x8 low-frequency block -> 64-bit hash
_PHASH_HIGHFREQ = 4      # DCT input is (size * highfreq)^2 = 32x32

Progress = Callable[[int, int | None], None]
FetchOriginal = Callable[[Row], Path]

_HEIF_REGISTERED = False


def _ensure_heif() -> None:
    """Register pillow-heif's HEIC/HEIF opener with Pillow (once)."""
    global _HEIF_REGISTERED
    if _HEIF_REGISTERED:
        return
    try:
        from pillow_heif import register_heif_opener
    except ImportError:
        _HEIF_REGISTERED = True  # not installed; treat as done, decode will fail loudly
        return
    register_heif_opener()
    _HEIF_REGISTERED = True


def compute_sha256(path: Path) -> str:
    with path.open("rb") as fh:
        return hashlib.file_digest(fh, "sha256").hexdigest()


def compute_phash(path: Path) -> str:
    """64-bit DCT perceptual hash as 16 hex chars.

    Standard pHash construction: grayscale, 32x32 resize, 2D DCT-II, threshold
    the 8x8 low-frequency block against its median. EXIF orientation is applied
    first so a rotated re-import of the same shot hashes identically. Compare
    hashes by hamming distance on the bits, never by equality alone.
    """
    from PIL import Image, ImageOps

    _ensure_heif()
    size = _PHASH_SIZE * _PHASH_HIGHFREQ
    with Image.open(path) as img:
        img = ImageOps.exif_transpose(img)
        gray = img.convert("L").resize((size, size), Image.LANCZOS)
    pixels = np.asarray(gray, dtype=np.float64)
    dct = scipy.fft.dct(scipy.fft.dct(pixels, axis=0), axis=1)
    low = dct[:_PHASH_SIZE, :_PHASH_SIZE]
    bits = (low > np.median(low)).flatten()
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return f"{value:0{_PHASH_SIZE * _PHASH_SIZE // 4}x}"


def phash_hamming(a_hex: str, b_hex: str) -> int:
    """Hamming distance between two 16-hex-char (64-bit) DCT pHashes.

    pHashes are near-, not exactly-equal for visually similar images, so
    duplicate detection compares them by bit distance rather than string
    equality (see `compute_phash`).
    """
    return (int(a_hex, 16) ^ int(b_hex, 16)).bit_count()


def _fetch_work(conn: Connection, space: Space) -> list[Row]:
    # `IS NOT` is SQLite's null-safe comparison: a photo whose cache_key
    # changed on the NAS (edited/replaced) gets re-hashed.
    return conn.execute(
        "SELECT * FROM photos WHERE space = ? AND deleted = 0 "
        "AND type IN ('photo', 'live') "
        "AND (sha256 IS NULL OR hash_cache_key IS NOT cache_key) "
        "ORDER BY id",
        (space,),
    ).fetchall()


def sync_hashes(
    conn: Connection,
    fetch_original: FetchOriginal,
    space: Space,
    progress: Progress | None = None,
) -> dict:
    """Hash every not-yet-hashed (or since-edited) photo in `space`.

    Videos are excluded: downloading them just to hash is expensive and a
    frame-less perceptual hash would be meaningless.
    """
    work = _fetch_work(conn, space)
    total = len(work)
    hashed = 0
    failed = 0

    for i, row in enumerate(work, start=1):
        try:
            path = fetch_original(row)
            sha256 = compute_sha256(path)
            try:
                phash = compute_phash(path)
            except Exception:
                log.warning("phash failed for %s/%s (%s); storing sha256 only",
                            space, row["id"], row["filename"], exc_info=True)
                phash = None
            conn.execute(
                "UPDATE photos SET sha256 = ?, phash = ?, hash_cache_key = ?, "
                "hashed_at = ? WHERE space = ? AND id = ?",
                (sha256, phash, row["cache_key"], store.now(), space, row["id"]),
            )
            conn.commit()
            hashed += 1
        except Exception:
            log.warning("hashing failed for %s/%s (%s); continuing",
                        space, row["id"], row["filename"], exc_info=True)
            failed += 1
        if progress and (i % 25 == 0 or i == total):
            progress(i, total)

    return {"hashed": hashed, "failed": failed}
