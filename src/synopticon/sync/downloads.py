"""Original-image cache: sharded on-disk layout, atomic download, LRU eviction."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from ..db import Connection

from synopticon.config import Settings, Space
from synopticon.syno import foto
from synopticon.syno.client import SynoClient

log = logging.getLogger(__name__)

_DEFAULT_EXT = ".jpg"


def original_path(settings: Settings, photo_row) -> Path:
    """`{originals_dir}/{id % 256:02x}/{space}_{id}_{cache_key}{ext}`."""
    pid = int(photo_row["id"])
    space = photo_row["space"]
    cache_key = photo_row["cache_key"] or "nokey"
    filename = photo_row["filename"] or ""
    ext = Path(filename).suffix.lower() or _DEFAULT_EXT
    shard = f"{pid % 256:02x}"
    return settings.storage.originals_dir / shard / f"{space}_{pid}_{cache_key}{ext}"


def ensure_original(
    conn: Connection, client: SynoClient, settings: Settings, photo_row
) -> Path:
    """Download the original if not already cached; atomic `.part` -> rename."""
    final_path = original_path(settings, photo_row)
    if final_path.exists():
        return final_path

    final_path.parent.mkdir(parents=True, exist_ok=True)
    part_path = final_path.with_name(final_path.name + ".part")

    space: Space = photo_row["space"]
    unit_id = photo_row["unit_id"] if photo_row["unit_id"] is not None else photo_row["id"]
    cache_key = photo_row["cache_key"]

    written = 0
    try:
        with part_path.open("wb") as fh:
            for chunk in foto.download_original(client, space, unit_id, cache_key):
                fh.write(chunk)
                written += len(chunk)

        expected = photo_row["filesize"]
        if expected is not None and written != expected:
            log.warning(
                "ensure_original: size mismatch for %s/%s: expected %s, wrote %s",
                space, photo_row["id"], expected, written,
            )

        os.replace(part_path, final_path)
    finally:
        if part_path.exists() and not final_path.exists():
            part_path.unlink(missing_ok=True)

    return final_path


def evict_originals(settings: Settings, keep_bytes: int | None = None) -> dict:
    """LRU-by-atime eviction under `originals_cache_gb`; no-op when `keep_originals`."""
    if settings.storage.keep_originals:
        return {"evicted_files": 0, "freed_bytes": 0}

    limit = keep_bytes if keep_bytes is not None else int(settings.storage.originals_cache_gb * 1e9)
    originals_dir = settings.storage.originals_dir
    if not originals_dir.exists():
        return {"evicted_files": 0, "freed_bytes": 0}

    entries: list[tuple[float, int, Path]] = []
    total = 0
    for path in originals_dir.rglob("*"):
        if not path.is_file():
            continue
        st = path.stat()
        atime = st.st_atime or st.st_mtime
        entries.append((atime, st.st_size, path))
        total += st.st_size

    entries.sort(key=lambda e: e[0])

    evicted_files = 0
    freed_bytes = 0
    for _atime, size, path in entries:
        if total <= limit:
            break
        try:
            path.unlink()
        except OSError:
            continue
        total -= size
        freed_bytes += size
        evicted_files += 1

    return {"evicted_files": evicted_files, "freed_bytes": freed_bytes}
