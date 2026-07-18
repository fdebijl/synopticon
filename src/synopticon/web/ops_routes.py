"""Read-only API routes for the Pipeline / Apply / Maintenance pages.

Registered onto the main app by :func:`register_ops_routes`, which
``web/app.py`` calls once (a single import + call before ``return app``) so the
big app factory stays untouched. Everything here is DB-only and NAS-free:

* ``GET /api/review/named-merge-pairs`` — the approved named↔named merge pairs,
  used to populate the Apply page's typed-phrase confirmation dialog (the same
  warning list the CLI's ``apply-all`` prints).
* ``GET /api/maintenance/counts`` — "what will be removed" counts for the
  Maintenance cards (pending queue, crop disk usage, row counts, approved
  corrections by kind).

The heavy ``pipeline.crops`` import is done lazily and every failure degrades to
``null`` disk-usage figures rather than 500ing the endpoint (a fresh install
without model/runtime deps must still render the Maintenance page).
"""

from __future__ import annotations

import sqlite3
from typing import Callable

from ..config import Settings


def _count(conn: sqlite3.Connection, table: str) -> int:
    try:
        row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
        return int(row["n"])
    except sqlite3.Error:
        return 0


def register_ops_routes(
    app,
    settings: Settings,
    conn: Callable[[], sqlite3.Connection],
) -> None:
    """Attach the ops (pipeline/apply/maintenance) read-only API to ``app``.

    ``conn`` is the per-request connection factory from ``create_app`` (opens a
    fresh ``store.connect`` each call; the caller closes it in try/finally).
    """
    from pathlib import Path

    from ..review import queries

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
            data = {
                "pending_queue": pending,
                "approved_by_kind": approved,
                "photos": _count(c, "photos"),
                "faces": _count(c, "faces"),
                "embeddings": _count(c, "embeddings"),
                "cluster_runs": _count(c, "cluster_runs"),
                "crops": _crops_usage(Path(settings.storage.crops_dir)),
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
