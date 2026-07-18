"""Dashboard statistics gathered from the pipeline DB (NAS-free, read-only).

Pure ``sqlite3`` + :class:`~synopticon.config.Settings`; no ``syno``/``pipeline``
import at module scope. The one place that needs the model manifest
(``pipeline_version`` / extract coverage) imports it lazily and wraps every
failure so a fresh install with no model weights degrades to
``models_ready: false`` and ``pipeline_version: null`` instead of crashing the
``/api/stats`` endpoint.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..config import Settings


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _photo_stats(conn: sqlite3.Connection, spaces: list[str]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for space in spaces:
        row = conn.execute(
            "SELECT "
            "  COUNT(*) AS total, "
            "  COALESCE(SUM(CASE WHEN deleted = 0 THEN 1 ELSE 0 END), 0) AS synced, "
            "  COALESCE(SUM(CASE WHEN sha256 IS NOT NULL AND deleted = 0 THEN 1 ELSE 0 END), 0) AS hashed, "
            "  COALESCE(SUM(CASE WHEN deleted = 1 THEN 1 ELSE 0 END), 0) AS deleted "
            "FROM photos WHERE space = ?",
            (space,),
        ).fetchone()
        out[space] = {
            "total": int(row["total"]),
            "synced": int(row["synced"]),
            "hashed": int(row["hashed"]),
            "deleted": int(row["deleted"]),
        }
    return out


def _extract_stats(conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
    """Extract coverage against the current pipeline_version.

    Degrades to ``pipeline_version: null`` / ``models_ready: false`` when the
    model manifest is absent or any manifest/import error occurs — never raises.
    """
    models_ready = False
    version: str | None = None
    try:
        from ..pipeline.manifest import load_manifest

        manifest = load_manifest(settings.storage.models_dir)
        if manifest:
            from ..pipeline.runner import pipeline_version

            version = pipeline_version(settings, settings.storage.models_dir)
            models_ready = True
    except Exception:  # noqa: BLE001 - a missing/broken manifest must not 500
        models_ready = False
        version = None

    # Photos eligible for extraction: not deleted, not video.
    eligible = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM photos "
            "WHERE deleted = 0 AND (type IS NULL OR type != 'video')"
        ).fetchone()["n"]
    )
    processed: int | None = None
    coverage: float | None = None
    if models_ready and version is not None:
        processed = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM extract_log WHERE pipeline_version = ?",
                (version,),
            ).fetchone()["n"]
        )
        coverage = (processed / eligible) if eligible else None
    return {
        "pipeline_version": version,
        "models_ready": models_ready,
        "eligible": eligible,
        "processed": processed,
        "coverage": coverage,
    }


def _cluster_stats(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT run_id, created_at FROM cluster_runs ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    clusters = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM clusters WHERE run_id = ?", (row["run_id"],)
        ).fetchone()["n"]
    )
    return {
        "run_id": int(row["run_id"]),
        "created_at": row["created_at"],
        "clusters": clusters,
    }


def gather_stats(conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
    """Assemble the dashboard stats payload. DB-only; never contacts the NAS."""
    spaces = list(settings.nas.spaces) or ["personal"]
    faces = int(conn.execute("SELECT COUNT(*) AS n FROM faces").fetchone()["n"])
    embeddings = int(
        conn.execute("SELECT COUNT(*) AS n FROM embeddings").fetchone()["n"]
    )

    from ..review import queries  # DB-only, no fastapi import

    return {
        "photos": _photo_stats(conn, spaces),
        "faces": faces,
        "embeddings": embeddings,
        "extract": _extract_stats(conn, settings),
        "cluster": _cluster_stats(conn),
        "review": queries.queue_counts(conn),
    }
