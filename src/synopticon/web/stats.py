"""Dashboard statistics gathered from the pipeline DB (NAS-free, read-only).

Pure :mod:`synopticon.db` + :class:`~synopticon.config.Settings`; no
``syno``/``pipeline`` import at module scope. The one place that needs the model
manifest (``pipeline_version`` / extract coverage) imports it lazily and wraps every
failure so a fresh install with no model weights degrades to
``models_ready: false`` and ``pipeline_version: null`` instead of crashing the
``/api/stats`` endpoint.

Those lazy imports must stay confined to the *leaf* pipeline modules
(``pipeline.manifest``, ``pipeline.version``). Reaching for ``pipeline.runner``
instead drags numpy + cv2 into a request handler on the only uvicorn process —
see :func:`_pipeline_version_cached`.
"""

from __future__ import annotations

from typing import Any

from ..config import Settings
from ..db import Connection


def _photo_stats(conn: Connection, spaces: list[str]) -> dict[str, dict[str, int]]:
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


_VERSION_CACHE: dict[Any, tuple[bool, str | None]] = {}


def _pipeline_version_cached(settings: Settings) -> tuple[bool, str | None]:
    """``(models_ready, pipeline_version)``, memoized on the manifest's identity.

    The version comes from ``pipeline.version`` — a leaf module — and **never**
    from ``pipeline.runner``: runner imports numpy + cv2 at module scope, so the
    first ``/api/stats`` after a restart used to page both shared objects in
    inside a request handler (sub-second warm, seconds on cold NAS storage) just
    to sha256 the manifest. A stall that long on the only uvicorn process shows
    up client-side as every unrelated in-flight request finishing at the same
    instant.

    The memo survives for a second reason: it also skips re-reading the manifest
    on every dashboard poll. The cache key is the manifest's (mtime_ns, size), so
    swapping models still invalidates it.
    """
    from ..pipeline.manifest import manifest_path

    try:
        st = manifest_path(settings.storage.models_dir).stat()
        key = (str(settings.storage.models_dir), st.st_mtime_ns, st.st_size)
    except OSError:
        return False, None  # no manifest: nothing imported, nothing to cache

    cached = _VERSION_CACHE.get(key)
    if cached is not None:
        return cached

    try:
        from ..pipeline.manifest import load_manifest

        result: tuple[bool, str | None] = (False, None)
        if load_manifest(settings.storage.models_dir):
            from ..pipeline.version import pipeline_version

            result = (True, pipeline_version(settings, settings.storage.models_dir))
    except Exception:  # noqa: BLE001 - a missing/broken manifest must not 500
        result = (False, None)
    _VERSION_CACHE.clear()  # only ever one live manifest
    _VERSION_CACHE[key] = result
    return result


def _extract_stats(conn: Connection, settings: Settings) -> dict[str, Any]:
    """Extract coverage against the current pipeline_version.

    Degrades to ``pipeline_version: null`` / ``models_ready: false`` when the
    model manifest is absent or any manifest/import error occurs — never raises.
    """
    models_ready, version = _pipeline_version_cached(settings)

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


def _cluster_stats(conn: Connection) -> dict[str, Any] | None:
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


def gather_stats(conn: Connection, settings: Settings) -> dict[str, Any]:
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
