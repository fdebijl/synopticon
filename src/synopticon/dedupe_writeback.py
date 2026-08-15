"""Delete duplicate photos from Synology Photos (the state-changing half of `dedupe`).

Kept separate from `syno/writeback.py` (whose `SynoWriter`/`DryRunWriter` speak
the Person API — assign/merge/reassign/…). Photo deletion is a different API
entirely: `SYNO.Foto[Team].BackgroundTask.File` / `delete` / v1, HAR-verified in
`har/deleting_one_photo.har` and `har/deleting_multiple_photos.har`:

    item_id=[103212,...]  folder_id=[]
    -> {"task_info": {"operation": "delete", "status": "waiting", "total": N}}

The call queues an async background task; `success:true` means *queued*, not
*done* (no status-poll was captured), so we audit the returned `task_info` and
mark local state optimistically.

Safety, mirroring `apply_reviewed`:
- each id is re-checked against the live NAS (`get_item`) immediately before
  deletion — a `LookupError` means it's already gone (idempotent skip);
- every attempt (dry-run, success, failure) lands in `audit_log`;
- dry-run records `dryrun.delete` rows and never touches `photos.deleted`.
"""

from __future__ import annotations

import logging
from typing import Iterable

from .db import Connection

from synopticon import audit
from synopticon.config import Space
from synopticon.progress import get_emitter
from synopticon.syno import foto
from synopticon.syno.client import SynoApiError, SynoClient

# Reuse the write-back / apply logger so `configure_apply_logging` routes dedupe
# deletions into the same apply.log as person write-backs.
log = logging.getLogger("synopticon.apply")

# Max item ids per delete call. The API accepts a batch (verified with 2 ids);
# chunk conservatively so one call never carries an unbounded id list.
_DELETE_BATCH = 100


def _mark_deleted(conn: Connection, space: Space, item_ids: Iterable[int]) -> None:
    ids = list(item_ids)
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    conn.execute(
        f"UPDATE photos SET deleted = 1 WHERE space = ? AND id IN ({placeholders})",
        (space, *ids),
    )
    conn.commit()


def delete_items(
    conn: Connection,
    client: SynoClient | None,
    space: Space,
    item_ids: Iterable[int],
    *,
    dry_run: bool,
) -> dict:
    """Delete `item_ids` from `space`. Returns `{deleted, skipped, failed}` counts.

    `client` may be None when `dry_run` is set. Orphaned `faces`/`embeddings`/
    `syno_faces` rows are intentionally left behind — every downstream query
    filters `deleted = 0`, so they're inert.
    """
    ids = list(dict.fromkeys(int(i) for i in item_ids))  # de-dup, preserve order
    api = None if client is None else client.api_name(space, "BackgroundTask.File")

    if dry_run:
        for chunk in (ids[i : i + _DELETE_BATCH] for i in range(0, len(ids), _DELETE_BATCH)):
            log.info("dryrun delete %s: item_id=%s", space, chunk)
            audit.record(
                conn,
                action="dryrun.delete",
                api="BackgroundTask.File.delete",
                params={"space": space, "item_id": chunk, "folder_id": []},
                success=None,
            )
        return {"deleted": 0, "skipped": 0, "failed": 0}

    assert client is not None, "delete_items requires a client unless dry_run"

    # Idempotency pre-check: only delete ids the NAS still has.
    emitter = get_emitter()
    live: list[int] = []
    skipped = 0
    failed = 0
    for idx, item_id in enumerate(ids):
        emitter.progress("dedupe.delete", idx + 1, len(ids), space=space)
        try:
            foto.get_item(client, space, item_id)
            live.append(item_id)
        except LookupError:
            log.info("delete %s/%s: already gone, skipping", space, item_id)
            skipped += 1
        except SynoApiError as exc:
            log.warning("delete %s/%s: pre-check failed (code %s), skipping", space, item_id, exc.code)
            failed += 1

    deleted = 0
    for chunk in (live[i : i + _DELETE_BATCH] for i in range(0, len(live), _DELETE_BATCH)):
        params = {"item_id": chunk, "folder_id": []}
        try:
            data = client.call(api, "delete", version=1, **params)
            task_info = (data or {}).get("task_info")
            log.info("delete %s: item_id=%s -> %s", space, chunk, task_info)
            audit.record(
                conn,
                action="dedupe.delete",
                api=f"{api}.delete",
                params={**params, "version": 1},
                response=data,
                success=True,
            )
            _mark_deleted(conn, space, chunk)
            deleted += len(chunk)
        except SynoApiError as exc:
            log.warning("delete %s: item_id=%s FAILED (code %s)", space, chunk, exc.code)
            audit.record(
                conn,
                action="dedupe.delete",
                api=f"{api}.delete",
                params={**params, "version": 1},
                response=None,
                success=False,
            )
            failed += len(chunk)

    return {"deleted": deleted, "skipped": skipped, "failed": failed}
