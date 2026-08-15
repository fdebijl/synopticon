"""Setup-wizard API routes: install status, NAS test-connection, storage checks.

Registered by :func:`create_app` through :func:`register_setup_routes` so
``app.py`` carries only a single call rather than these route bodies (keeps the
concurrent-edit surface on ``app.py`` minimal).

Reachability: all three endpoints live under ``/api/setup/*``. The auth
middleware in ``app.py`` already exempts that prefix *only during first boot*
(before any admin account exists); once a user exists they require a session /
API key like every other ``/api`` endpoint. This module does not loosen that —
it simply relies on it.

Safety: nothing here is persisted and nothing mutates NAS state.
``test-connection`` builds an in-memory :class:`Settings` from the candidate NAS
dict and runs the read-only :func:`~synopticon.syno.probe.probe`; the only side
effect is that a successful login stores the 2FA device token in ``sync_state``
(intended — future logins skip the OTP). Persisting the NAS config itself is the
job of ``PUT /api/config`` (a separate endpoint), not this module.
"""

import os
import shutil
from pathlib import Path
from typing import Any, Callable

from ..db import Connection, errors as db_errors


def register_setup_routes(
    app,
    *,
    settings,
    conn: Callable[[], Connection],
    auth,
) -> None:
    """Attach the setup-wizard routes to ``app``.

    ``conn`` is the per-request connection factory from ``create_app``; ``auth``
    is :mod:`synopticon.web.auth` (for ``has_users``).
    """
    from fastapi import Request
    from starlette.concurrency import run_in_threadpool

    from ..config import load_settings
    from ..syno.probe import probe

    @app.get("/api/setup/status")
    def api_setup_status():
        c = conn()
        try:
            return _status(c, settings, auth)
        finally:
            c.close()

    @app.post("/api/setup/test-connection")
    async def api_setup_test_connection(request: Request):
        body = await request.json()

        def work():
            candidate = load_settings(nas=_nas_overrides(body))
            c = conn()
            try:
                return probe(candidate, c)
            finally:
                c.close()

        # A blocking HTTPS round-trip to the NAS. On the event loop an
        # unreachable host would freeze the whole GUI for the full timeout.
        result = await run_in_threadpool(work)
        return result.to_dict()

    @app.post("/api/setup/check-storage")
    async def api_setup_check_storage(request: Request):
        body = await request.json()
        # Touches the filesystem (mkdir / free-space probe).
        return await run_in_threadpool(_check_storage, body)


# --------------------------------------------------------------------------- #
# status                                                                        #
# --------------------------------------------------------------------------- #
def _status(c: Connection, settings, auth) -> dict[str, Any]:
    from ..config import _config_file

    nas = settings.nas
    nas_configured = bool(
        nas.url.strip() and nas.account.strip() and nas.password.get_secret_value()
    )
    cf = _config_file()
    storage = settings.storage
    missing = _models_missing(settings)
    return {
        "config_file": str(cf) if cf else None,
        "nas_configured": nas_configured,
        "models_ready": not missing,
        "models_missing": missing,
        # An external database is reachable by definition here: `c` came from it.
        "db_exists": (
            Path(storage.db_path).exists()
            if settings.database.backend == "sqlite"
            else True
        ),
        "photos_synced": _count(c, "SELECT COUNT(*) FROM photos WHERE deleted = 0"),
        "extract_done": _count(c, "SELECT COUNT(*) FROM extract_log"),
        "cluster_runs": _count(c, "SELECT COUNT(*) FROM cluster_runs"),
        "account_created": auth.has_users(c),
        # Prefill hints for the wizard's NAS + storage steps (not secrets).
        "nas": {
            "url": nas.url,
            "account": nas.account,
            "verify_tls": nas.verify_tls,
            "spaces": list(nas.spaces),
        },
        "storage": {
            "data_dir": str(storage.data_dir),
            "models_dir": str(storage.models_dir),
            "keep_originals": storage.keep_originals,
            "originals_cache_gb": storage.originals_cache_gb,
        },
    }


def _count(c: Connection, sql: str) -> int:
    try:
        row = c.execute(sql).fetchone()
    except db_errors.OperationalError:
        # Rolling back is what makes the *next* _count call work: PostgreSQL
        # aborts the transaction on a failed statement, so one missing table
        # would otherwise zero every count after it. No-op on SQLite.
        c.rollback()
        return 0
    return int(row[0]) if row else 0


def _models_missing(settings) -> list[str]:
    """The canonical required-model keys whose weight file is absent on disk.

    Checks disk presence of every model in ``REQUIRED_MODELS`` (not just the
    ones the manifest happens to list) so a *partial* download never reports
    ready. Wraps every failure (import error, unreadable dir) into "all
    missing" so a fresh install never crashes the wizard.
    """
    try:
        from ..pipeline.manifest import missing_models

        return missing_models(settings.storage.models_dir)
    except Exception:  # noqa: BLE001 - never let a manifest problem 500 the wizard
        try:
            from ..pipeline.manifest import REQUIRED_MODELS

            return list(REQUIRED_MODELS)
        except Exception:  # noqa: BLE001
            return ["scrfd_10g_bnkps", "yolov8l-face", "glintr100",
                    "adaface_ir101_webface12m", "magface_iresnet100"]


# --------------------------------------------------------------------------- #
# test-connection                                                               #
# --------------------------------------------------------------------------- #
def _nas_overrides(body: dict) -> dict[str, Any]:
    """Build a NasConfig init-override dict from wizard form fields."""
    nas: dict[str, Any] = {
        "url": (body.get("url") or "").strip(),
        "account": (body.get("account") or "").strip(),
        "password": body.get("password") or "",
        "verify_tls": bool(body.get("verify_tls", True)),
        "otp_code": (body.get("otp_code") or None),
    }
    spaces = [s for s in (body.get("spaces") or []) if s in ("personal", "shared")]
    if spaces:
        nas["spaces"] = spaces
    return nas


# --------------------------------------------------------------------------- #
# check-storage                                                                 #
# --------------------------------------------------------------------------- #
def _check_storage(body: dict) -> dict[str, Any]:
    dirs: dict[str, Any] = {}
    ok = True
    for key in ("data_dir", "models_dir"):
        entry = _check_dir(body.get(key))
        dirs[key] = entry
        if not entry["ok"]:
            ok = False
    return {"ok": ok, "dirs": dirs}


def _check_dir(path: Any) -> dict[str, Any]:
    if not path:
        return {"ok": False, "detail": "no path provided", "free_gb": None}
    p = Path(str(path)).expanduser()
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "detail": f"cannot create: {exc}", "free_gb": None}
    free_gb = _free_gb(p)
    if not os.access(p, os.W_OK):
        return {"ok": False, "detail": "not writable", "free_gb": free_gb}
    return {"ok": True, "detail": "writable", "free_gb": free_gb}


def _free_gb(p: Path) -> float | None:
    try:
        return round(shutil.disk_usage(p).free / (1024**3), 1)
    except OSError:
        return None
