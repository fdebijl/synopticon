"""Read-only API routes for the Pipeline / Apply / Maintenance pages.

Registered onto the main app by :func:`register_ops_routes`, which
``web/app.py`` calls once (a single import + call before ``return app``) so the
big app factory stays untouched. Everything here is DB-only and NAS-free:

* ``GET /api/review/named-merge-pairs`` — the approved named↔named merge pairs,
  used to populate the Apply page's typed-phrase confirmation dialog (the same
  warning list the CLI's ``apply-all`` prints).
* ``GET /api/maintenance/counts`` — "what will be removed" counts for the
  Maintenance cards (pending queue, queued applies, crop disk usage, row
  counts, approved corrections by kind).

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
                "photos": _count(c, "photos"),
                "faces": _count(c, "faces"),
                "embeddings": _count(c, "embeddings"),
                "cluster_runs": _count(c, "cluster_runs"),
                "crops": crops_usage(Path(settings.storage.crops_dir)),
            }
            return data
        finally:
            c.close()


def _crops_usage(crops_dir) -> dict:
    """Crop file count + bytes, degrading to nulls on any error (missing deps,
    unreadable dir, ...) so the Maintenance page never 500s."""
    try:
        from ..pipeline import crops

        files, nbytes = crops.crops_disk_usage(crops_dir)
        return {"files": int(files), "bytes": int(nbytes)}
    except Exception:  # noqa: BLE001 - disk-usage is advisory, never fatal
        return {"files": None, "bytes": None}
