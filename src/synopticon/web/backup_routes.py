"""Download endpoints for the Utilities page's two backups.

Everything here is read-only and NAS-free:

* ``GET /api/backup/info`` — what the two cards show before you click: where
  the config file lives, which database backend is configured, how big the
  SQLite file is.
* ``GET /api/backup/config`` — ``config.toml`` as a download. Credentials are
  blanked unless ``?secrets=1`` says otherwise; see
  :func:`~synopticon.web.configio.export_config`.
* ``GET /api/backup/database`` — a SQLite snapshot of the whole library, built
  by :mod:`synopticon.db.snapshot` for whichever backend is configured.

Deliberately not jobs. A job's product is a log, and these produce a file the
browser has to receive on the same request; ``JOB_SPECS`` has nothing to say
about a download. Both handlers are sync ``def``, so FastAPI runs them on a
worker thread and the snapshot never touches the event loop (ADR 07).

No ``from __future__ import annotations`` in this module: it would degrade the
``Request`` parameters to required query fields and 422 every call (same trap
as ``quickmerger.py`` and ``schedule_routes.py``).
"""

import logging
import shutil
import tempfile
import time
import threading
from pathlib import Path
from typing import Callable

from ..config import Settings
from ..db import Connection

log = logging.getLogger("synopticon.web")

#: Prefix for the working directory a snapshot is built in, under `data_dir`
#: (the volume with room for a second copy of the database — a container's
#: `/tmp` usually is not).
_WORKDIR_PREFIX = ".snapshot-"

#: A workdir older than this lost its download; the next build removes it.
_STALE_AFTER = 3600.0


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _sweep(parent: Path) -> None:
    """Remove snapshot workdirs left behind by an interrupted download."""
    cutoff = time.time() - _STALE_AFTER
    try:
        candidates = list(parent.glob(_WORKDIR_PREFIX + "*"))
    except OSError:
        return
    for path in candidates:
        try:
            if path.is_dir() and path.stat().st_mtime < cutoff:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:  # noqa: PERF203 - a sweep never fails the request
            continue


def register_backup_routes(
    app,
    settings: Settings,
    conn: Callable[[], Connection],
) -> None:
    """Attach the backup download API to ``app``.

    ``conn`` is ``create_app``'s per-request connection factory, used only to
    write the audit rows.
    """
    from fastapi import Request
    from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
    from starlette.background import BackgroundTask

    from .. import audit
    from ..db import snapshot as db_snapshot
    from . import configio

    # Serializes snapshot *construction* only. It is released before the file
    # starts streaming, so a client that hangs up mid-download cannot wedge the
    # button for everyone; the abandoned workdir is `_sweep`'s problem.
    building = threading.Lock()

    def _who(request: Request) -> str | None:
        ident = getattr(request.state, "ident", None)
        return f"{ident[0]}:{ident[1]}" if ident else None

    def _audit(action: str, params: dict, success: bool = True) -> None:
        c = None
        try:
            c = conn()
            audit.record(c, action=action, params=params, success=success)
        except Exception:  # noqa: BLE001 - a backup must not fail on bookkeeping
            log.warning("could not record %s in the audit log", action, exc_info=True)
        finally:
            if c is not None:
                c.close()

    @app.get("/api/backup/info")
    def api_backup_info():
        target = configio.config_target(settings)
        return {
            "config": {
                "path": str(target),
                "exists": target.is_file(),
                "secret_keys": [f"{s}.{k}" for s, k in configio.secret_paths(settings)],
            },
            "database": {
                "backend": settings.database.backend,
                "bytes": db_snapshot.source_bytes(settings),
            },
        }

    @app.get("/api/backup/config")
    def api_backup_config(request: Request, secrets: bool = False):
        ident = getattr(request.state, "ident", None)
        if not ident or ident[0] != "user":
            return JSONResponse(
                {
                    "error": "A backup has to be downloaded from a signed-in browser, "
                    "not with an access key."
                },
                status_code=403,
            )
        try:
            text = configio.export_config(settings, include_secrets=secrets)
        except ImportError as exc:
            return JSONResponse({"error": str(exc)}, status_code=503)
        # Worth a row either way: one says a credential left the box, the other
        # is the only record that someone took a copy of the settings at all.
        _audit("backup.config", {"secrets": bool(secrets), "by": _who(request)})
        name = f"synopticon-config-{_stamp()}.toml"
        return PlainTextResponse(
            text,
            media_type="application/toml",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    @app.get("/api/backup/database")
    def api_backup_database(request: Request):
        ident = getattr(request.state, "ident", None)
        if not ident or ident[0] != "user":
            return JSONResponse(
                {
                    "error": "A backup has to be downloaded from a signed-in browser, "
                    "not with an access key."
                },
                status_code=403,
            )
        if not building.acquire(blocking=False):
            return JSONResponse(
                {"error": "a database backup is already being prepared"},
                status_code=409,
            )
        parent = Path(settings.storage.data_dir)
        workdir = None
        try:
            parent.mkdir(parents=True, exist_ok=True)
            _sweep(parent)
            workdir = Path(tempfile.mkdtemp(dir=parent, prefix=_WORKDIR_PREFIX))
            dest = workdir / f"synopticon-backup-{_stamp()}.db"
            db_snapshot.snapshot(settings, dest)
        except FileNotFoundError:
            if workdir is not None:
                shutil.rmtree(workdir, ignore_errors=True)
            return JSONResponse(
                {"error": "there is no database to back up yet"}, status_code=404
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the user as 500
            log.exception("database snapshot failed")
            if workdir is not None:
                shutil.rmtree(workdir, ignore_errors=True)
            _audit("backup.database", {"error": str(exc)}, success=False)
            return JSONResponse(
                {"error": f"could not build the snapshot: {exc}"}, status_code=500
            )
        finally:
            building.release()

        _audit(
            "backup.database",
            {
                "backend": settings.database.backend,
                "bytes": dest.stat().st_size,
                "by": _who(request),
            },
        )
        return FileResponse(
            dest,
            media_type="application/vnd.sqlite3",
            filename=dest.name,
            background=BackgroundTask(shutil.rmtree, workdir, ignore_errors=True),
        )
