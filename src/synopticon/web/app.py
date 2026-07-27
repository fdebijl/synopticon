"""The Synopticon web GUI: ``create_app(settings)`` + ``serve(...)``.

fastapi/uvicorn are imported lazily behind :func:`_require_fastapi` so the
package (and the ``synopticon`` CLI) imports without the ``[review]`` extra. The
app is a thin FastAPI wrapper over the framework-free building blocks landed in
Wave 1:

* :mod:`synopticon.web.jobs` — the allowlisted, consent-gated subprocess runner.
* :mod:`synopticon.web.auth` — users, sessions, API keys, login rate limiting.
* :mod:`synopticon.review.queries` — review-queue data layer (shared with the
  legacy ``review/app.py``).
* :mod:`synopticon.web.stats` — dashboard stats (DB-only, NAS-free).

Auth model (see the web-GUI plan §7):

* Session cookie (HttpOnly, SameSite=Lax, ``Secure`` when the request scheme is
  https — honoured behind a reverse proxy via uvicorn ``--proxy-headers``).
* ``Authorization: Bearer syn_...`` API-key path for ``/api`` (immune to CSRF by
  construction — no cookie involved).
* First boot (no users yet): only ``/setup``, ``/api/setup/*`` and
  ``/api/auth/create-account`` are reachable; everything else 302s to ``/setup``.
* Unauthenticated page -> 302 ``/login``; unauthenticated ``/api`` -> 401 JSON.
* CSRF hardening: mutating ``/api`` endpoints require ``Content-Type:
  application/json`` (the JSON login/logout endpoints live under ``/api`` and
  pass that gate naturally).

SPA serving: the frontend is a Vue 3 SPA built by Vite into ``web/dist``
(``index.html`` + content-hashed ``assets/*`` + favicons/manifest at the dist
root). ``/assets`` is served *publicly* (hashed, no user data) so the login and
setup views can load their bundle before a session exists; the dist-root files
(favicons, ``site.webmanifest``, ``img/``) are likewise public. Everything else
routes through the catch-all, which returns ``index.html`` to authenticated page
requests. ``/crops`` are personal photos and require a session like every page.
"""

import asyncio
import json
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..config import Settings
from ..db import store

_DIST_DIR = Path(__file__).parent / "dist"

SESSION_COOKIE = "synopticon_session"
_MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled", "interrupted"})


def _require_fastapi():
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError as exc:  # pragma: no cover - trivial guard
        raise ImportError(
            "The web GUI needs the [review] extra: pip install 'synopticon[review]'"
        ) from exc


def _package_version() -> str:
    try:
        from importlib.metadata import version

        return version("synopticon")
    except Exception:  # noqa: BLE001
        return "dev"


def _check_dist_built(dist_dir: Path) -> None:
    """Fail fast (``SystemExit``) if the SPA bundle is missing.

    Called by :func:`serve`; the API-only tests bypass it via the ``dist_dir``
    seam + the 503 catch-all fallback, so pytest never needs a built bundle.
    """
    if not (dist_dir / "index.html").is_file():
        raise SystemExit(
            "The web GUI frontend is not built. Build the Docker image, or run:\n"
            "  cd frontend && npm ci && npm run build\n"
            f"(expected {dist_dir / 'index.html'})"
        )


def create_app(
    settings: Settings,
    *,
    job_manager: Any | None = None,
    dist_dir: Path | None = None,
):
    """Build the web GUI FastAPI app. Requires the [review] extra.

    ``job_manager`` is an injection seam for tests (pass a :class:`JobManager`
    built with fake specs / a stub command builder so no real subprocess runs).
    ``dist_dir`` overrides the built-SPA location (defaults to :data:`_DIST_DIR`)
    so tests can inject a stub bundle — ``create_app`` works even when the dir is
    absent (the catch-all then returns a 503).
    """
    _require_fastapi()
    from fastapi import FastAPI, Request
    from fastapi.responses import (
        FileResponse,
        JSONResponse,
        RedirectResponse,
        StreamingResponse,
    )
    from fastapi.staticfiles import StaticFiles
    from starlette.concurrency import run_in_threadpool

    from . import auth
    from ..review import queries
    from .jobs import (
        ConsentError,
        JobManager,
        JobParamError,
        QueueFullError,
    )
    from .stats import gather_stats

    db_path = Path(settings.storage.db_path)
    crops_dir = Path(settings.storage.crops_dir)
    crops_dir.mkdir(parents=True, exist_ok=True)
    # Ensure the schema (incl. migration 0006 web tables) exists before serving.
    store.connect(db_path).close()

    jobs_dir = Path(settings.storage.data_dir) / "jobs"
    jm = job_manager if job_manager is not None else JobManager(jobs_dir)

    dist = Path(dist_dir) if dist_dir is not None else _DIST_DIR
    dist_root = dist.resolve()
    app_version = _package_version()
    limiter = auth.LoginRateLimiter()

    @asynccontextmanager
    async def lifespan(app):
        yield
        jm.shutdown()

    app = FastAPI(title="Synopticon", lifespan=lifespan)
    # /assets public (hashed SPA bundle, no user data); mount only if the built
    # dist exists so create_app works without a build. /crops is guarded by auth.
    assets_dir = dist / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")
    app.mount("/crops", StaticFiles(directory=str(crops_dir)), name="crops")

    def conn() -> sqlite3.Connection:
        return store.connect(db_path)

    def _resolve_dist_file(path: str) -> Path | None:
        """Resolve ``path`` to a real file inside the dist root, or ``None``.

        Hardened against traversal: the candidate is ``resolve()``d and must stay
        under ``dist_root``. Returns ``None`` when the dist is absent, the path is
        empty/``/``, escapes the root, or does not name an existing file.
        """
        rel = path.lstrip("/")
        if not rel:
            return None
        candidate = (dist / rel).resolve()
        if not candidate.is_relative_to(dist_root):
            return None
        return candidate if candidate.is_file() else None

    # -- auth helpers ------------------------------------------------------- #
    def _authenticate(request: Request, c: sqlite3.Connection):
        """Return ``("user", id)`` / ``("apikey", id)`` or ``None``."""
        header = request.headers.get("authorization", "")
        if header.startswith("Bearer "):
            kid = auth.validate_api_key(c, header[len("Bearer ") :].strip())
            if kid is not None:
                return ("apikey", kid)
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            uid = auth.validate_session(c, token)
            if uid is not None:
                return ("user", uid)
        return None

    # Once an account exists it can never be removed (there is no delete-user
    # route), so first boot is a one-way latch: cache it and stop querying.
    have_users = False

    def _auth_lookup(request: Request) -> tuple[Any, bool]:
        """Blocking auth resolution: ``(ident, first_boot)``.

        Runs in the threadpool — never on the event loop. Opening a SQLite
        connection and reading from it can block for as long as the DB lock is
        held, and doing that on the loop stalls every other in-flight request
        with it (a single slow query becomes a server-wide stall).
        """
        nonlocal have_users
        c = conn()
        try:
            if not have_users:
                have_users = auth.has_users(c)
            return _authenticate(request, c), not have_users
        finally:
            c.close()

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        path = request.url.path
        method = request.method
        # Hashed SPA bundle: public so /login and /setup can load before a
        # session exists (mirrors the old /static bypass).
        if path.startswith("/assets/"):
            return await call_next(request)
        # Dist-root files (favicons, site.webmanifest, img/…) are public too —
        # the login view needs them unauthenticated. index.html is NOT bypassed:
        # it follows the page auth rules below (only /login and /setup unauth'd).
        # /api paths can never name a dist file, so skip the stat() for them.
        if (
            not path.startswith("/api/")
            and path != "/index.html"
            and _resolve_dist_file(path) is not None
        ):
            return await call_next(request)

        ident, first_boot = await run_in_threadpool(_auth_lookup, request)
        request.state.ident = ident
        request.state.first_boot = first_boot

        is_api = path.startswith("/api/")
        # CSRF: mutating API calls must be JSON (cookie is SameSite=Lax; a
        # cross-site form post cannot set application/json).
        if is_api and method in _MUTATING:
            ctype = request.headers.get("content-type", "").split(";")[0].strip()
            if ctype != "application/json":
                return JSONResponse(
                    {"error": "Content-Type must be application/json"},
                    status_code=415,
                )

        if first_boot:
            allowed = (
                path == "/setup"
                or path.startswith("/api/setup")
                or path == "/api/auth/create-account"
                # /api/auth/me must answer during first boot so the SPA router
                # guard can detect the claim flow (returns first_boot: true).
                or path == "/api/auth/me"
            )
            if allowed:
                return await call_next(request)
            if is_api:
                return JSONResponse(
                    {"error": "setup required", "setup": True}, status_code=403
                )
            return RedirectResponse("/setup", status_code=302)

        # Users exist. /login serves the SPA shell without a session so the
        # LoginView can render (its client-side guard handles the rest).
        if path == "/login":
            return await call_next(request)
        # SPA auth endpoints reachable without a session: /api/auth/me reports
        # auth state (always 200), and the JSON login accepts credentials. Both
        # still pass the CSRF Content-Type gate above (login is a mutating POST).
        if path == "/api/auth/me":
            return await call_next(request)
        if path == "/api/auth/login" and method == "POST":
            return await call_next(request)
        if path == "/api/auth/create-account":
            return JSONResponse({"error": "account already exists"}, status_code=403)

        if ident is None:
            if is_api:
                return JSONResponse(
                    {"error": "authentication required"}, status_code=401
                )
            return RedirectResponse(f"/login?next={quote(path)}", status_code=302)
        return await call_next(request)

    # -- auth helpers ------------------------------------------------------- #
    def _set_session_cookie(resp, token: str, request: Request) -> None:
        """Attach the session cookie (HttpOnly, SameSite=Lax, 30d).

        ``Secure`` follows the effective scheme so a TLS-terminating reverse
        proxy (uvicorn ``--proxy-headers``) gets a Secure cookie over https.
        """
        resp.set_cookie(
            SESSION_COOKIE,
            token,
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
            max_age=30 * 86400,
            path="/",
        )

    # -- auth: create-account / JSON login / logout / me (SPA) -------------- #
    @app.post("/api/auth/create-account")
    async def api_create_account(request: Request):
        # Reachable only during first boot (middleware blocks it afterwards).
        body = await request.json()
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        if not username or not password:
            return JSONResponse(
                {"error": "username and password are required"}, status_code=422
            )
        c = conn()
        try:
            if auth.has_users(c):
                return JSONResponse(
                    {"error": "account already exists"}, status_code=403
                )
            uid = auth.create_user(c, username, password)
            token = auth.create_session(c, uid)
        except auth.UsernameTakenError:
            return JSONResponse({"error": "username taken"}, status_code=409)
        finally:
            c.close()
        resp = JSONResponse({"ok": True, "user_id": uid}, status_code=201)
        _set_session_cookie(resp, token, request)
        return resp

    # -- auth: JSON login / logout / me (SPA) ------------------------------- #
    @app.post("/api/auth/login")
    async def api_login(request: Request):
        body = await request.json()
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        ip = request.client.host if request.client else "?"
        if not limiter.check(ip, username):
            return JSONResponse(
                {"error": "Too many attempts — try again shortly."}, status_code=429
            )
        c = conn()
        try:
            uid = auth.verify_password(c, username, password)
            if uid is None:
                limiter.record_failure(ip, username)
                return JSONResponse(
                    {"error": "Invalid username or password."}, status_code=401
                )
            limiter.record_success(ip, username)
            token = auth.create_session(c, uid)
        finally:
            c.close()
        resp = JSONResponse({"ok": True, "username": username})
        _set_session_cookie(resp, token, request)
        return resp

    @app.post("/api/auth/logout")
    def api_logout(request: Request):
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            c = conn()
            try:
                auth.delete_session(c, token)
            finally:
                c.close()
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(SESSION_COOKIE, path="/")
        return resp

    @app.get("/api/auth/me")
    def api_me(request: Request):
        """Auth state for the SPA router guard. Always 200 (even unauthenticated
        and during first boot) — allowlisted in both middleware branches."""
        first_boot = bool(getattr(request.state, "first_boot", False))
        ident = getattr(request.state, "ident", None)
        username = None
        authenticated = ident is not None
        if ident and ident[0] == "user":
            c = conn()
            try:
                row = c.execute(
                    "SELECT username FROM web_users WHERE id = ?", (ident[1],)
                ).fetchone()
                username = row["username"] if row else None
            finally:
                c.close()
        return {
            "authenticated": authenticated,
            "username": username,
            "first_boot": first_boot,
            "version": app_version,
        }

    # -- API: stats / audit ------------------------------------------------- #
    @app.get("/api/stats")
    def api_stats():
        c = conn()
        try:
            data = gather_stats(c, settings)
        finally:
            c.close()
        running = [j for j in jm.list_jobs() if j["state"] in ("queued", "running")]
        history = jm.history(limit=1)
        data["job"] = {
            "current": running[0] if running else None,
            "last": history[0] if history else None,
        }
        return data

    @app.get("/api/models")
    def api_models():
        from ..pipeline import manifest as mf

        models_dir = settings.storage.models_dir
        try:
            items = mf.model_status(models_dir)
        except Exception:
            items = []
        return {"models_dir": str(models_dir), "items": items}

    @app.get("/api/audit")
    def api_audit(limit: int = 50):
        from .. import audit

        c = conn()
        try:
            rows = audit.tail(c, limit=max(1, min(int(limit), 500)))
            return {"items": [dict(r) for r in rows]}
        finally:
            c.close()

    # -- API: jobs ---------------------------------------------------------- #
    @app.post("/api/jobs")
    async def api_submit_job(request: Request):
        body = await request.json()
        name = body.get("name")
        params = body.get("params") or {}
        confirm = bool(body.get("confirm", False))
        confirm_phrase = body.get("confirm_phrase")
        if not isinstance(name, str) or not name:
            return JSONResponse({"error": "missing job name"}, status_code=422)
        try:
            job_id = jm.submit(
                name, params, confirm=confirm, confirm_phrase=confirm_phrase
            )
        except ConsentError as exc:
            # 428 Precondition Required. Never leak the expected phrase text.
            return JSONResponse(
                {
                    "error": "consent required",
                    "requirement": exc.requirement,
                    "field": exc.field,
                    "detail": exc.detail,
                },
                status_code=428,
            )
        except QueueFullError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except JobParamError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        return JSONResponse({"job_id": job_id}, status_code=202)

    @app.get("/api/jobs")
    def api_list_jobs():
        return {"items": jm.history(limit=50)}

    @app.get("/api/jobs/{job_id}")
    def api_get_job(job_id: str):
        meta = jm.get(job_id)
        if meta is None:
            return JSONResponse({"error": "unknown job"}, status_code=404)
        return meta

    @app.get("/api/jobs/{job_id}/events")
    def api_job_events(job_id: str, after: int = 0):
        meta = jm.get(job_id)
        if meta is None:
            return JSONResponse({"error": "unknown job"}, status_code=404)
        return {
            "events": jm.events(job_id, after=int(after)),
            "state": meta.get("state"),
            "seq": meta.get("seq"),
        }

    @app.get("/api/jobs/{job_id}/stream")
    async def api_job_stream(request: Request, job_id: str, after: int = 0):
        if jm.get(job_id) is None:
            return JSONResponse({"error": "unknown job"}, status_code=404)

        # Must be an *async* generator. A sync one is iterated via
        # ``iterate_in_threadpool``, so every open stream would sit on an AnyIO
        # worker thread between yields (up to the 15 s ping) — and that pool is
        # shared with every sync route handler. A few dozen streams then starve
        # the whole API. Here the waiting is an ``asyncio.sleep`` that holds no
        # thread; ``jm.events``/``jm.get`` are in-memory and lock-bounded.
        async def event_stream():
            last = int(after)
            last_ping = time.monotonic()
            while True:
                # A client that navigated away (or a proxy that dropped the
                # connection) must not leave this loop spinning forever.
                if await request.is_disconnected():
                    return
                drained = jm.events(job_id, after=last)
                for evt in drained:
                    last = evt.get("seq", last)
                    yield f"id: {last}\ndata: {json.dumps(evt)}\n\n"
                    if evt.get("event") == "final":
                        return
                meta = jm.get(job_id)
                if meta is None:
                    return
                if meta.get("state") in _TERMINAL_STATES and not jm.events(
                    job_id, after=last
                ):
                    # Terminal with no synthesized `final` (e.g. adopted orphan).
                    yield (
                        f"data: {json.dumps({'event': 'final', 'state': meta['state']})}\n\n"
                    )
                    return
                now = time.monotonic()
                if now - last_ping >= 15:
                    last_ping = now
                    yield ": ping\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/jobs/{job_id}/cancel")
    def api_cancel_job(job_id: str):
        ok = jm.cancel(job_id)
        if not ok:
            return JSONResponse(
                {"error": "job unknown or already finished"}, status_code=404
            )
        return {"ok": True}

    # -- API: review -------------------------------------------------------- #
    @app.get("/api/review/items")
    def api_review_items(
        kind: str = "", status: str = "pending", limit: int = 100, offset: int = 0
    ):
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        c = conn()
        try:
            items = queries.load_review_items(
                c, settings, kind=kind, status=status, limit=limit, offset=offset
            )
            total = queries.count_review_items(c, kind=kind, status=status)
        finally:
            c.close()
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    @app.get("/api/review/counts")
    def api_review_counts():
        c = conn()
        try:
            return {"counts": queries.queue_counts(c)}
        finally:
            c.close()

    @app.post("/api/review/{item_id}/decide")
    async def api_review_decide(request: Request, item_id: int):
        body = await request.json()
        decision = body.get("decision")
        c = conn()
        try:
            if decision == "undo":
                new_status = queries.undo_decision(c, item_id)
                if new_status is None:
                    return JSONResponse(
                        {
                            "error": "cannot undo: item is not in an "
                            "approved/rejected state"
                        },
                        status_code=409,
                    )
                return {"item_id": item_id, "status": new_status}
            new_status = queries.decide_item(c, item_id, decision)
        finally:
            c.close()
        if new_status is None:
            return JSONResponse({"error": "bad decision"}, status_code=400)
        return {"item_id": item_id, "status": new_status}

    @app.post("/api/review/bulk")
    async def api_review_bulk(request: Request):
        body = await request.json()
        kind = body.get("kind")
        if not kind:
            return JSONResponse({"error": "kind is required"}, status_code=422)
        try:
            min_conf = float(body.get("min_confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            return JSONResponse(
                {"error": "min_confidence must be a number"}, status_code=422
            )
        c = conn()
        try:
            approved = queries.bulk_approve(c, kind, min_confidence=min_conf)
        finally:
            c.close()
        return {"approved": approved}

    @app.post("/api/review/{item_id}/name")
    async def api_review_name(request: Request, item_id: int):
        body = await request.json()
        name = (body.get("name") or "").strip()
        c = conn()
        try:
            ok = queries.set_suggested_name(c, item_id, name)
        finally:
            c.close()
        if not ok:
            return JSONResponse({"error": "not a new_person item"}, status_code=400)
        return {"item_id": item_id, "suggested_name": name}

    from .ops_routes import register_ops_routes

    register_ops_routes(app, settings, conn)

    from .setup_routes import register_setup_routes

    register_setup_routes(app, settings=settings, conn=conn, auth=auth)

    from .configio import register_config_routes

    register_config_routes(app, settings, conn, jm)

    # -- SPA shell (catch-all, registered LAST) ----------------------------- #
    @app.get("/{path:path}")
    def spa_catch_all(request: Request, path: str):
        """Serve the built Vue SPA.

        Ordering matters: every real API/mount/route is registered above, so
        this only fires for unmatched paths. An unknown ``/api/...`` path must
        never fall through to ``index.html`` (a typo'd API call gets JSON 404,
        not HTML). Otherwise a real dist file (favicon, manifest, img/…) is
        served directly, else the SPA shell; a missing bundle is a 503 so the
        API-only test suite runs without a Node build.
        """
        if path.startswith("api/"):
            return JSONResponse({"error": "not found"}, status_code=404)
        real = _resolve_dist_file(path)
        if real is not None:
            return FileResponse(real)
        index = dist / "index.html"
        if index.is_file():
            return FileResponse(index)
        return JSONResponse(
            {
                "error": "frontend not built",
                "hint": "cd frontend && npm ci && npm run build",
            },
            status_code=503,
        )

    app.state.job_manager = jm
    return app


def serve(settings: Settings, host: str = "127.0.0.1", port: int = 8686) -> None:
    """Run the web GUI. Requires the [review] extra.

    ``proxy_headers`` is enabled so a TLS-terminating reverse proxy can pass
    ``X-Forwarded-Proto``; the session cookie's ``Secure`` flag then follows the
    effective scheme.
    """
    _require_fastapi()
    import uvicorn

    _check_dist_built(_DIST_DIR)
    app = create_app(settings)
    uvicorn.run(
        app,
        host=host,
        port=port,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
