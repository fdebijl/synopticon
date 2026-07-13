"""Sync photo ground truth (`photos` + `person_photos`) from Synology.

Each pass fully replaces per-photo state (row + its person_photos rows) from
the fresh fetch -- no partial-merge logic, correctness over cleverness. Photos
no longer seen this pass are marked `deleted=1`; photos that reappear are
un-marked automatically by the per-item upsert.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from synopticon.config import Space
from synopticon.db import store
from synopticon.syno import foto
from synopticon.syno.client import SynoClient

_CHUNK = 500
_PROGRESS_EVERY = 50

Progress = Callable[[int, int | None], None]


def sync_items(
    conn: sqlite3.Connection,
    client: SynoClient,
    space: Space,
    progress: Progress | None = None,
) -> dict:
    seen: set[int] = set()
    upserted = 0
    now = store.now()

    for item in foto.list_items(client, space):
        seen.add(item.id)
        conn.execute(
            "INSERT INTO photos (id, space, filename, folder_id, filesize, time, "
            "indexed_time, type, cache_key, unit_id, width, height, orientation, "
            "synced_at, deleted) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0) "
            "ON CONFLICT(space, id) DO UPDATE SET "
            "filename = excluded.filename, folder_id = excluded.folder_id, "
            "filesize = excluded.filesize, time = excluded.time, "
            "indexed_time = excluded.indexed_time, type = excluded.type, "
            "cache_key = excluded.cache_key, unit_id = excluded.unit_id, "
            "width = excluded.width, height = excluded.height, "
            "orientation = excluded.orientation, synced_at = excluded.synced_at, deleted = 0",
            (
                item.id, space, item.filename, item.folder_id, item.filesize, item.time,
                item.indexed_time, item.type, item.cache_key, item.unit_id,
                item.width, item.height, item.orientation, now,
            ),
        )
        upserted += 1

        conn.execute("DELETE FROM person_photos WHERE space = ? AND photo_id = ?", (space, item.id))
        for pid in item.person_ids:
            conn.execute(
                "INSERT INTO person_photos (space, person_id, photo_id, source, synced_at) "
                "VALUES (?, ?, ?, 'synology', ?)",
                (space, pid, item.id, now),
            )
        conn.commit()
        if progress and upserted % _PROGRESS_EVERY == 0:
            progress(upserted, None)

    if progress:
        progress(upserted, None)

    existing_ids = [
        int(row["id"])
        for row in conn.execute(
            "SELECT id FROM photos WHERE space = ? AND deleted = 0", (space,)
        ).fetchall()
    ]
    to_mark = [pid for pid in existing_ids if pid not in seen]
    deleted = 0
    for start in range(0, len(to_mark), _CHUNK):
        chunk = to_mark[start : start + _CHUNK]
        placeholders = ",".join("?" for _ in chunk)
        conn.execute(
            f"UPDATE photos SET deleted = 1 WHERE space = ? AND id IN ({placeholders})",
            (space, *chunk),
        )
        deleted += len(chunk)
    conn.commit()

    return {"seen": len(seen), "upserted": upserted, "deleted": deleted}


def stale_photo_ids(conn: sqlite3.Connection, space: Space) -> list[int]:
    """Photo ids with no rows at all in `faces` for this space.

    Cheap, correct-today subset of "needs pipeline attention" -- there is no
    other cache_key snapshot to compare against yet. A future `pipeline/`
    migration can extend this once `faces` tracks a processed cache_key,
    without changing this function's signature.
    """
    rows = conn.execute(
        "SELECT id FROM photos WHERE space = ? AND deleted = 0 AND id NOT IN "
        "(SELECT DISTINCT photo_id FROM faces WHERE space = ?)",
        (space, space),
    ).fetchall()
    return [int(row["id"]) for row in rows]
