"""The Synopticon web GUI: ``create_app(settings)`` + ``serve(...)``.

fastapi/uvicorn/jinja2 are imported lazily behind :func:`_require_fastapi` so the
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
* First boot (no users yet): only ``/setup`` pages, ``/api/setup/*`` and
  ``/api/auth/create-account`` are reachable; everything else 302s to ``/setup``.
* Unauthenticated page -> 302 ``/login``; unauthenticated ``/api`` -> 401 JSON.
* CSRF hardening: mutating ``/api`` endpoints require ``Content-Type:
  application/json`` (the login/logout form endpoints live outside ``/api``).

Static-asset policy: ``/static`` (our own css/js, no user data) is served
*publicly* so the login and setup pages can style themselves before a session
exists. ``/crops`` are personal photos and require a session like every other
page. This is the simplest correct split; revisit if static ever holds user data.
"""

import json
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..config import Settings
from ..db import store

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"

SESSION_COOKIE = "synopticon_session"
_MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled", "interrupted"})


def _require_fastapi():
    try:
        import fastapi  # noqa: F401
        import jinja2  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError as exc:  # pragma: no cover - trivial guard
        raise ImportError(
            "The web GUI needs the [review] extra: pip install 'synopticon[review]'"
        ) from exc


def _asset_version() -> str:
    try:
        from importlib.metadata import version

        return version("synopticon")
    except Exception:  # noqa: BLE001
        return "dev"


def create_app(settings: Settings, *, job_manager: Any | None = None):
    """Build the web GUI FastAPI app. Requires the [review] extra.

    ``job_manager`` is an injection seam for tests (pass a :class:`JobManager`
    built with fake specs / a stub command builder so no real subprocess runs).
    """
    _require_fastapi()
    from fastapi import FastAPI, Request
    from fastapi.responses import (
        HTMLResponse,
        JSONResponse,
        RedirectResponse,
        StreamingResponse,
    )
    from fastapi.staticfiles import StaticFiles
    from jinja2 import Environment, FileSystemLoader, select_autoescape

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

    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "j2"]),
    )
    asset_version = _asset_version()
    limiter = auth.LoginRateLimiter()

    @asynccontextmanager
    async def lifespan(app):
        yield
        jm.shutdown()

    app = FastAPI(title="Synopticon", lifespan=lifespan)
    # /static public (own assets); /crops guarded by the auth middleware.
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
    app.mount("/crops", StaticFiles(directory=str(crops_dir)), name="crops")

    def conn() -> sqlite3.Connection:
        return store.connect(db_path)

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

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        path = request.url.path
        method = request.method
        # Own css/js: public so /login and /setup can style themselves.
        if path.startswith("/static/"):
            return await call_next(request)

        c = conn()
        try:
            first_boot = not auth.has_users(c)
            ident = _authenticate(request, c)
        finally:
            c.close()
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
            )
            if allowed:
                return await call_next(request)
            if is_api:
                return JSONResponse(
                    {"error": "setup required", "setup": True}, status_code=403
                )
            return RedirectResponse("/setup", status_code=302)

        # Users exist. Login/logout live outside the auth gate.
        if path == "/login" or (path == "/logout" and method == "POST"):
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

    # -- rendering ---------------------------------------------------------- #
    def _base_ctx(request: Request, active: str, title: str) -> dict:
        pending = 0
        username = None
        c = conn()
        try:
            counts = queries.queue_counts(c)
            pending = sum((counts.get("pending") or {}).values())
            ident = getattr(request.state, "ident", None)
            if ident and ident[0] == "user":
                row = c.execute(
                    "SELECT username FROM web_users WHERE id = ?", (ident[1],)
                ).fetchone()
                username = row["username"] if row else None
        finally:
            c.close()
        running = [j for j in jm.list_jobs() if j["state"] in ("queued", "running")]
        return {
            "active": active,
            "title": title,
            "version": asset_version,
            "pending_review": pending,
            "username": username,
            "running_job": running[0] if running else None,
        }

    def _safe_next(target: str | None) -> str:
        """Only allow same-site relative redirects (defeat open-redirect via ?next)."""
        if target and target.startswith("/") and not target.startswith("//"):
            return target
        return "/"

    def render(name: str, request: Request, active: str, title: str, **ctx):
        base = _base_ctx(request, active, title)
        base.update(ctx)
        return HTMLResponse(env.get_template(name).render(**base))

    # -- pages -------------------------------------------------------------- #
    @app.get("/", response_class=HTMLResponse)
    def page_dashboard(request: Request):
        from .. import audit

        c = conn()
        try:
            stats = gather_stats(c, settings)
            audit_rows = [dict(r) for r in audit.tail(c, limit=20)]
        finally:
            c.close()
        synced = sum(
            int(p.get("synced", 0)) for p in (stats.get("photos") or {}).values()
        )
        empty = synced == 0 and int(stats.get("faces", 0)) == 0
        return render(
            "index.html.j2",
            request,
            "dashboard",
            "Dashboard",
            stats=stats,
            audit=audit_rows,
            empty=empty,
        )

    @app.get("/pipeline", response_class=HTMLResponse)
    def page_pipeline(request: Request):
        return render("pipeline.html.j2", request, "pipeline", "Pipeline")

    @app.get("/review", response_class=HTMLResponse)
    def page_review(request: Request, kind: str = "", status: str = "pending"):
        first_page = 100
        c = conn()
        try:
            items = queries.load_review_items(
                c, settings, kind=kind, status=status, limit=first_page, offset=0
            )
            total = queries.count_review_items(c, kind=kind, status=status)
        finally:
            c.close()
        return render(
            "review.html.j2",
            request,
            "review",
            "Review",
            kind=kind,
            status=status,
            items=items,
            total=total,
            page_size=first_page,
        )

    @app.get("/apply", response_class=HTMLResponse)
    def page_apply(request: Request):
        return render("apply.html.j2", request, "apply", "Apply")

    @app.get("/maintenance", response_class=HTMLResponse)
    def page_maintenance(request: Request):
        return render("maintenance.html.j2", request, "maintenance", "Maintenance")

    @app.get("/settings", response_class=HTMLResponse)
    def page_settings(request: Request):
        return render("settings.html.j2", request, "settings", "Settings")

    @app.get("/setup", response_class=HTMLResponse)
    def page_setup(request: Request):
        return render(
            "setup.html.j2",
            request,
            "setup",
            "Setup",
            first_boot=getattr(request.state, "first_boot", False),
        )

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def page_job(request: Request, job_id: str):
        return render("job.html.j2", request, "", "Job", job_id=job_id)

    # -- auth: login / logout / create-account ------------------------------ #
    @app.get("/login", response_class=HTMLResponse)
    def page_login(request: Request, next: str = "/"):
        next = _safe_next(next)
        if getattr(request.state, "ident", None) is not None:
            return RedirectResponse(next, status_code=302)
        return render("login.html.j2", request, "", "Sign in", next=next, error=None)

    @app.post("/login")
    async def do_login(request: Request):
        form = await request.form()
        username = (form.get("username") or "").strip()
        password = form.get("password") or ""
        next_url = _safe_next(form.get("next"))
        ip = request.client.host if request.client else "?"

        def login_error(msg: str, code: int):
            base = _base_ctx(request, "", "Sign in")
            base.update(next=next_url, error=msg)
            return HTMLResponse(
                env.get_template("login.html.j2").render(**base), status_code=code
            )

        if not limiter.check(ip, username):
            return login_error("Too many attempts — try again shortly.", 429)
        c = conn()
        try:
            uid = auth.verify_password(c, username, password)
            if uid is None:
                limiter.record_failure(ip, username)
                return login_error("Invalid username or password.", 401)
            limiter.record_success(ip, username)
            token = auth.create_session(c, uid)
        finally:
            c.close()
        resp = RedirectResponse(next_url or "/", status_code=302)
        resp.set_cookie(
            SESSION_COOKIE,
            token,
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
            max_age=30 * 86400,
            path="/",
        )
        return resp

    @app.post("/logout")
    def do_logout(request: Request):
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            c = conn()
            try:
                auth.delete_session(c, token)
            finally:
                c.close()
        resp = RedirectResponse("/login", status_code=302)
        resp.delete_cookie(SESSION_COOKIE, path="/")
        return resp

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
        resp.set_cookie(
            SESSION_COOKIE,
            token,
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
            max_age=30 * 86400,
            path="/",
        )
        return resp

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
    def api_job_stream(job_id: str, after: int = 0):
        if jm.get(job_id) is None:
            return JSONResponse({"error": "unknown job"}, status_code=404)

        def event_stream():
            last = int(after)
            last_ping = time.monotonic()
            while True:
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
                time.sleep(0.5)

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

    app = create_app(settings)
    uvicorn.run(
        app,
        host=host,
        port=port,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
