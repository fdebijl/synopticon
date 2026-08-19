"""Read-only API routes for the Pipeline / Apply / Maintenance pages.

Registered onto the main app by :func:`register_ops_routes`, which
``web/app.py`` calls once (a single import + call before ``return app``) so the
big app factory stays untouched. Everything here is DB-only and NAS-free:

* ``GET /api/review/named-merge-pairs`` — the approved named↔named merge pairs,
  used to populate the Apply page's typed-phrase confirmation dialog (the same
  warning list the CLI's ``apply-all`` prints).
* ``GET /api/maintenance/counts`` — "what will be removed" counts for the
  Maintenance cards (pending queue, queued applies, orphaned queue rows, crop
  disk usage, row counts, approved corrections by kind).

The heavy ``pipeline.crops`` import is done lazily and every failure degrades to
``null`` disk-usage figures rather than 500ing the endpoint (a fresh install
without model/runtime deps must still render the Maintenance page).
"""

from __future__ import annotations

from typing import Callable

from ..db import Connection, errors as db_errors

from ..config import Settings

#: How long a crops disk-usage figure stays fresh (seconds).
_CROPS_TTL = 60.0

#: Change-detector for the orphan scan, which parses every `review_queue` payload
#: and then looks up the faces they name — too much work to redo per request on a
#: large queue. A wall-clock TTL is the wrong key here (see `review/lookups.py`):
#: the figure moves the instant a `prune-queue` job deletes rows, and the page
#: reloads its counts right after a job finishes, so a stale window would show the
#: user a count their own action just corrected. The `queue_counts` result is
#: folded in by the caller to catch a status changing in place, which no count or
#: extent over `review_queue` alone would see.
_ORPHAN_FINGERPRINT_SQL = """
SELECT
  (SELECT COUNT(*) FROM review_queue),
  (SELECT MAX(item_id) FROM review_queue),
  (SELECT COUNT(*) FROM faces),
  (SELECT MAX(face_id) FROM faces),
  (SELECT COUNT(*) FROM photos),
  (SELECT COALESCE(SUM(deleted), 0) FROM photos)
"""


def _count(conn: Connection, table: str) -> int:
    try:
        row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
        return int(row["n"])
    except db_errors.DatabaseError:
        # Rolling back is what makes the *next* _count call work: PostgreSQL
        # aborts the transaction on a failed statement, so a single missing
        # table would otherwise zero every count after it. No-op on SQLite.
        conn.rollback()
        return 0


def register_ops_routes(
    app,
    settings: Settings,
    conn: Callable[[], Connection],
) -> None:
    """Attach the ops (pipeline/apply/maintenance) read-only API to ``app``.

    ``conn`` is the per-request connection factory from ``create_app`` (opens a
    fresh ``store.connect`` each call; the caller closes it in try/finally).
    """
    import time
    from pathlib import Path

    from ..review import queries

    # `_crops_usage` stats every crop on disk — hundreds of thousands of files on
    # a populated library, inside a request handler. The figure is advisory and
    # the page re-polls, so cache it briefly rather than walk the tree per call.
    crops_cache: list[tuple[float, dict]] = []

    def crops_usage(crops_dir: Path) -> dict:
        now = time.monotonic()
        if crops_cache and now - crops_cache[0][0] < _CROPS_TTL:
            return crops_cache[0][1]
        data = _crops_usage(crops_dir)
        crops_cache.clear()
        crops_cache.append((now, data))
        return data

    orphans_cache: list[tuple[tuple, dict]] = []

    def orphan_counts(c: Connection, queue: dict) -> dict:
        key = _orphan_fingerprint(c, queue)
        if orphans_cache and orphans_cache[0][0] == key:
            return orphans_cache[0][1]
        data = _orphan_counts(c)
        orphans_cache.clear()
        orphans_cache.append((key, data))
        return data

    @app.get("/api/review/named-merge-pairs")
    def api_named_merge_pairs():
        c = conn()
        try:
            return {"pairs": queries.named_merge_pairs(c)}
        finally:
            c.close()

    @app.get("/api/maintenance/counts")
    def api_maintenance_counts():
        c = conn()
        try:
            counts = queries.queue_counts(c)
            pending = sum((counts.get("pending") or {}).values())
            approved = dict(counts.get("approved") or {})
            failed = dict(counts.get("failed") or {})
            data = {
                "pending_queue": pending,
                "approved_by_kind": approved,
                "queued_applies": {
                    "approved": sum(approved.values()),
                    "failed": sum(failed.values()),
                },
                "orphaned_queue": orphan_counts(c, counts),
                "photos": _count(c, "photos"),
                "faces": _count(c, "faces"),
                "embeddings": _count(c, "embeddings"),
                "cluster_runs": _count(c, "cluster_runs"),
                "crops": crops_usage(Path(settings.storage.crops_dir)),
            }
            return data
        finally:
            c.close()


def _orphan_fingerprint(conn: Connection, queue: dict) -> tuple:
    """Cache key for the orphan scan: table extents plus the status histogram."""
    histogram = tuple(
        (status, kind, n)
        for status, kinds in sorted(queue.items())
        for kind, n in sorted(kinds.items())
    )
    try:
        return histogram + tuple(conn.execute(_ORPHAN_FINGERPRINT_SQL).fetchone())
    except db_errors.DatabaseError:
        # A missing table (fresh DB mid-migration) means "don't trust the cache".
        # The rollback is what lets the caller keep using this connection.
        conn.rollback()
        return (None, object())  # never equal to a previous key


def _orphan_counts(conn: Connection) -> dict:
    """``{status: count}`` of review rows whose faces are all gone, plus a
    ``prunable`` total for the statuses ``prune-queue`` clears by default.

    Degrades to ``{}`` rather than 500ing: the figure is advisory, and the rest of
    the Maintenance page must still render when the scan trips over anything.
    """
    try:
        from ..review import queries

        by_status = queries.orphan_counts(conn)
        prunable = sum(
            n for s, n in by_status.items() if s in queries.DEFAULT_PRUNE_STATUSES
        )
        return {"by_status": by_status, "prunable": prunable}
    except db_errors.DatabaseError:
        conn.rollback()  # see _count: PostgreSQL aborts the whole transaction
        return {}
    except Exception:  # noqa: BLE001 - an advisory count is never fatal
        return {}


def _crops_usage(crops_dir) -> dict:
    """Crop file count + bytes, degrading to nulls on any error (missing deps,
    unreadable dir, ...) so the Maintenance page never 500s."""
    try:
        from ..pipeline import crops

        files, nbytes = crops.crops_disk_usage(crops_dir)
        return {"files": int(files), "bytes": int(nbytes)}
    except Exception:  # noqa: BLE001 - disk-usage is advisory, never fatal
        return {"files": None, "bytes": None}
