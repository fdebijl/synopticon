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


def evict_originals(
    settings: Settings, keep_bytes: int | None = None, keep: Path | None = None
) -> dict:
    """LRU-by-atime eviction under `originals_cache_gb`; no-op when `keep_originals`.

    ``keep`` is never deleted, though it still counts toward the budget.
    """
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
        if path == keep:
            continue
        try:
            path.unlink()
        except OSError:
            continue
        total -= size
        freed_bytes += size
        evicted_files += 1

    return {"evicted_files": evicted_files, "freed_bytes": freed_bytes}


#: Bytes downloaded between eviction passes, as a fraction of the budget. Eviction
#: walks the whole cache, so it runs per slice rather than per photo; the cache
#: overshoots `originals_cache_gb` by at most one slice plus one original.
_EVICT_SLICE = 0.1


class BudgetedFetch:
    """`ensure_original` that holds the cache to `originals_cache_gb` during a run.

    The original just fetched is exempt from eviction, since the caller has yet
    to read it. Call :meth:`finish` once the run is over to trim to the budget
    and get the run's eviction totals.
    """

    def __init__(self, conn: Connection, client: SynoClient, settings: Settings):
        self._conn = conn
        self._client = client
        self._settings = settings
        budget = int(settings.storage.originals_cache_gb * 1e9)
        self._slice = max(1, int(budget * _EVICT_SLICE))
        self._pending = 0
        self.evicted = {"evicted_files": 0, "freed_bytes": 0}

    def __call__(self, photo_row) -> Path:
        cached = original_path(self._settings, photo_row).exists()
        path = ensure_original(self._conn, self._client, self._settings, photo_row)
        if cached or self._settings.storage.keep_originals:
            return path
        self._pending += path.stat().st_size
        if self._pending >= self._slice:
            self._pending = 0
            freed = self._evict(keep=path)
            if freed["evicted_files"]:
                log.info(
                    "originals cache over budget: evicted %d file(s), %.2f GB",
                    freed["evicted_files"], freed["freed_bytes"] / 1e9,
                )
        return path

    def finish(self) -> dict:
        self._evict(keep=None)
        return dict(self.evicted)

    def _evict(self, keep: Path | None) -> dict:
        freed = evict_originals(self._settings, keep=keep)
        for k, v in freed.items():
            self.evicted[k] += v
        return freed
