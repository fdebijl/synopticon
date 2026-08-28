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

* Session cookie (HttpOnly, SameSite=Lax, ``Secure`` when the effective scheme
  is https — resolved by Synopticon's own ``clientip.ProxyHeaders``, honoured
  only from an address listed in ``[security] trusted_proxies``; uvicorn's own
  proxy-header handling is switched off explicitly).
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
import html
import itertools
import json
import logging
import sys
import os
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..db import Connection

from . import auth, clientip
from ..config import Settings
from ..db import store

_DIST_DIR = Path(__file__).parent / "dist"

SESSION_COOKIE = auth.SESSION_COOKIE
_MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled", "interrupted"})

# Cache-Control for the three classes of static response. /assets filenames are
# content-hashed by Vite, so they can be cached forever; a rebuild emits new
# names. Crops are stable per face_id but `regen-crops` can rewrite one in
# place, so they get a day rather than a year. index.html must always be
# revalidated or a deploy never reaches an open tab.
_ASSETS_CACHE_CONTROL = "public, max-age=31536000, immutable"
_CROPS_CACHE_CONTROL = "public, max-age=86400"
_DIST_FILE_CACHE_CONTROL = "public, max-age=3600"
_SHELL_CACHE_CONTROL = "no-cache"

#: How long a validated *session cookie* stays trusted in memory. Every request
#: — including each of the ~100 crop images a review grid pulls — otherwise
#: opens its own SQLite connection just to resolve the cookie. API keys are
#: never cached (see ``_credential``), so their revocation stays exact.
#:
#: In-process revocation (logout, password change) clears the cache outright, so
#: this window only ever applies to a session revoked by *another* process —
#: ``synopticon reset-password``, which is a lockout-recovery path where waiting
#: out a cache is acceptable.
#:
#: It used to be 2 s, which is shorter than every polling interval the SPA has
#: (5 s for jobs while one runs, 15 s for review counts and the job list at
#: rest). The cache therefore never hit for the steady-state traffic that
#: dominates an idle GUI — every poll opened its own connection — and only ever
#: paid off during a crop burst. 30 s covers the pollers as well.
_AUTH_CACHE_TTL = 30.0

#: The auth cache's memory bound (F4). A stolen cookie replayed with N distinct
#: User-Agent values, now that the cache key folds in the client facts, mints N
#: entries; the insert site evicts the entry with the earliest deadline until
#: the cache is back under this cap. Never a wholesale `clear()` -- that would
#: convert the same flood into a tool for evicting every signed-in user's
#: verdict at once.
_AUTH_CACHE_MAX = 4096

#: Minimum gap between two "refused by network allowlist" log lines for the
#: same address (dict cleared past 1024 keys) -- a per-denied-request write
#: would otherwise make the allowlist itself a log-flooding vector for the
#: exact traffic it exists to reject.
_DENIED_LOG_INTERVAL = 60.0

#: The HTML denial page for a browser refused by the network allowlist (section
#: 5.4). A 403 here would otherwise loop back to /login, which is refused too.
_DENIED_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Access denied</title></head>
<body>
<p>This address is not allowed to reach Synopticon.</p>
<p>Your address: {ip}</p>
<pre>
To fix this from the server:

    synopticon web-access --clear

then restart the web process (docker compose restart synopticon) -- the
configuration is read once at start-up.
</pre>
</body></html>
"""

log = logging.getLogger("synopticon.web")

#: Responsiveness watchdog (see ``_watchdog`` in :func:`create_app`).
#:
#: The GUI is one uvicorn process, so *every* whole-server stall looks identical
#: from the outside: a batch of unrelated requests all completing at the same
#: instant, which is what a HAR shows and all it shows. There are three distinct
#: causes and picking between them after the fact is guesswork unless the server
#: recorded which one it was:
#:
#: 1. the event loop was blocked (sync work on the loop, or a worker thread
#:    holding the GIL) — shows up as loop *lag*;
#: 2. the AnyIO worker pool was saturated — every request needs a thread for
#:    ``_auth_lookup`` alone, so a full pool stalls the whole API with a
#:    perfectly healthy loop, and no lag is observed;
#: 3. one handler was genuinely slow and the reverse proxy serialised others
#:    behind it on a pooled upstream connection — no lag, no saturation, just
#:    one long request that the client may well have aborted (and so never
#:    appears in its own HAR — only its collateral damage does).
#:
#: The watchdog logs all three signals together, so the next occurrence is
#: attributable instead of re-derivable. It costs two ``monotonic()`` calls per
#: tick and only ever logs on pathology.
_WATCHDOG_TICK = 0.25
_WATCHDOG_STALL = 1.0
_SATURATION_LOG_INTERVAL = 10.0
#: Server-side handler wall time that earns a log line on its own.
_SLOW_REQUEST = 3.0
#: Per-path throttle for the slow-request line. A stall releases its whole queue
#: at once, so without this one incident logs a near-identical line per queued
#: request — burying the first (and only informative) one.
_SLOW_REQUEST_LOG_INTERVAL = 10.0
#: In-flight requests to name before collapsing the rest into "+N more". A
#: saturation incident can have hundreds; the oldest few are what matter.
_IN_FLIGHT_REPORT_MAX = 8
#: Completed requests remembered so a loop stall can name what ran during it.
_RECENT_DONE_MAX = 32


def _psi(resource: str) -> float | None:
    """Linux PSI ``some avg10`` for ``cpu``/``io``, or None where unavailable.

    "Percent of the last 10 s during which at least one task was stalled waiting
    for this resource" — exactly the question being asked when a request that
    does a millisecond of work takes a minute.
    """
    try:
        with open(f"/proc/pressure/{resource}", encoding="ascii") as fh:
            for line in fh:
                if not line.startswith("some "):
                    continue
                for field in line.split():
                    if field.startswith("avg10="):
                        return float(field[len("avg10=") :])
    except (OSError, ValueError):
        return None
    return None


def _pressure_report() -> str:
    """External-contention snapshot for a watchdog line.

    The three stall modes the watchdog already names are all *internal* — the
    loop, the pool, one slow handler. They cannot describe the fourth: the
    process was ready to run and the kernel did not schedule it, because a job
    subprocess had every core. That looks like perfect health from in here (no
    loop lag, an idle threadpool) while requests take 90 s, so the only way to
    tell it apart after the fact is to record what the machine was doing.

    Read lazily — only when something is already being logged.
    """
    parts = []
    for resource in ("cpu", "io"):
        value = _psi(resource)
        if value is not None:
            parts.append(f"{resource} stall {value:.0f}%")
    try:
        parts.append(f"load {os.getloadavg()[0]:.1f}/{os.cpu_count() or '?'}")
    except OSError:
        pass
    return ", ".join(parts) if parts else "pressure unavailable"


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


#: Paths served raw. Compressing them would burn event-loop CPU for no gain:
#: crops and inspect thumbnails are already-compressed JPEG/PNG, and a database
#: snapshot is mostly embedding blobs — gigabytes of incompressible float32
#: through a deflate stream that runs on the loop, not in a worker thread.
_NO_GZIP = ("/crops/", "/api/backup/database", "/api/inspect/image/")


def _add_gzip(app) -> None:
    """Compress everything except :data:`_NO_GZIP`.

    A 100-item review page is ~97 KiB of JSON that gzips to ~11 KiB, and the SPA
    bundle compresses similarly — hence the path guard rather than a bare
    ``add_middleware``. Starlette's GZipMiddleware already excludes
    ``text/event-stream``, so the job SSE stream keeps flushing event by event.
    """
    from starlette.middleware.gzip import GZipMiddleware

    class ConditionalGZip:
        def __init__(self, app):
            self.app = app
            self.gzip = GZipMiddleware(app, minimum_size=1024, compresslevel=5)

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http" and not scope["path"].startswith(_NO_GZIP):
                await self.gzip(scope, receive, send)
            else:
                await self.app(scope, receive, send)

    app.add_middleware(ConditionalGZip)


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
        HTMLResponse,
        JSONResponse,
        RedirectResponse,
        StreamingResponse,
    )
    from fastapi.staticfiles import StaticFiles
    from starlette.concurrency import run_in_threadpool

    from . import auth
    from ..review import queries
    from ..review.lookups import LookupCache
    from .jobs import (
        ConsentError,
        JobManager,
        JobParamError,
        QueueFullError,
    )
    from .stats import gather_stats

    class _CachedStatic(StaticFiles):
        """``StaticFiles`` that stamps a fixed ``Cache-Control`` on every hit.

        Without it the browser revalidates each asset and each crop on every
        navigation: a full round-trip per file to be told "304 Not Modified".
        """

        def __init__(self, *args, cache_control: str, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self._cache_control = cache_control

        def file_response(self, *args, **kwargs):
            response = super().file_response(*args, **kwargs)
            response.headers["Cache-Control"] = self._cache_control
            return response

    crops_dir = Path(settings.storage.crops_dir)
    crops_dir.mkdir(parents=True, exist_ok=True)
    # Ensure the schema (incl. migration 0006 web tables) exists before serving.
    store.connect(settings).close()

    jobs_dir = Path(settings.storage.data_dir) / "jobs"
    # Jobs share this machine with the server that launched them. Unconstrained,
    # a clustering run's BLAS pool takes every core and the GUI goes unusable —
    # see JobManager's `_THREAD_ENV_VARS` / `_renice`.
    jm = (
        job_manager
        if job_manager is not None
        else JobManager(
            jobs_dir,
            thread_cap=settings.inference.job_threads,
            nice=settings.inference.job_nice,
        )
    )

    dist = Path(dist_dir) if dist_dir is not None else _DIST_DIR
    dist_root = dist.resolve()
    app_version = _package_version()

    # Construction only -- _AuthMiddleware and api_login (defined further down
    # in this function) close over these locals. The app.state publication that
    # exposes them to routes in other modules happens after `app` is bound,
    # below.
    trusted_proxies = clientip.parse_networks(settings.security.trusted_proxies)
    allowlist = clientip.IPAllowlist(
        settings.security.allow_from, settings.security.allow_private_networks
    )
    limiter = auth.LoginRateLimiter(
        ip_max_failures=settings.security.max_failures_per_address,
        ip_window_seconds=settings.security.address_window_minutes * 60,
        ip_block_seconds=settings.security.address_block_minutes * 60,
    )
    auth.authlog.configure(
        enabled=settings.security.sign_in_log,
        max_age_days=settings.security.sign_in_log_days,
        max_rows=settings.security.sign_in_log_max_entries,
    )

    if settings.security.allow_from and not settings.security.trusted_proxies:
        log.warning(
            "[security] allow_from is set but [security] trusted_proxies is empty. If "
            "Synopticon sits behind a reverse proxy, every visitor arrives as the proxy's "
            "own address: the address list will restrict nobody, and per-address sign-in "
            "limits will count all your visitors as one. Set [security] trusted_proxies to "
            "the proxy's address or subnet, configure that proxy to OVERWRITE "
            "X-Forwarded-For (nginx: proxy_set_header X-Forwarded-For $remote_addr;), and "
            "restart. Then check Settings -> Access."
        )
    if any(
        net.version == lb.version and net.overlaps(lb)
        for net in trusted_proxies
        for lb in clientip.LOOPBACK
    ):
        log.warning(
            "[security] trusted_proxies lists a loopback address, so Synopticon believes "
            "the X-Forwarded-For header on any connection from this machine. That proxy "
            "MUST OVERWRITE the header -- nginx: proxy_set_header X-Forwarded-For "
            "$remote_addr; Caddy and Traefik do this by default. A proxy that merely "
            "passes the visitor's own X-Forwarded-For through (which is what a bare "
            "`proxy_pass` with no proxy_set_header line does, because nginx relays client "
            "headers by default) lets ANY visitor claim ANY address: the address list "
            "becomes bypassable with one header and per-address sign-in limits stop "
            "working. If the proxy forwards no address at all, every visitor instead "
            "arrives as 127.0.0.1 and shares one budget. Listing loopback without "
            "overwriting the header is strictly worse than not listing it. Check "
            "Settings -> Access."
        )

    # -- responsiveness watchdog ------------------------------------------- #
    # `in_flight` and `recently_done` are only ever touched from the event loop
    # (the timing middleware and the watchdog both run there), so they need no
    # lock.
    in_flight: dict[int, tuple[str, float]] = {}
    #: (label, started, ended) for the last few completed requests.
    #:
    #: A request that blocks the *loop* cannot be observed while it does so —
    #: the watchdog is a loop task, and by the time it runs again the culprit
    #: has returned and left `in_flight`. Reporting only what is in flight
    #: "now" therefore names everything except the one request that matters.
    #: Keeping a short tail lets a stall report what overlapped its window.
    recently_done: deque[tuple[str, float, float]] = deque(maxlen=_RECENT_DONE_MAX)
    request_seq = itertools.count()
    #: "METHOD <route template>" -> (last logged monotonic, suppressed since).
    #: Keyed on the *route*, not the URL, so a slow disk behind `/crops` cannot
    #: mint an entry per face crop (and a review page's worth of them all
    #: collapse into one log line, which is what you want to read anyway).
    slow_log_state: dict[str, tuple[float, int]] = {}
    #: address -> last time a "refused by network allowlist" WARNING was
    #: logged for it. Only ever touched from the event loop (the allowlist gate
    #: runs in _AuthMiddleware), so it needs no lock -- same reasoning as
    #: `in_flight` above.
    _denied_log_seen: dict[str, float] = {}

    def _in_flight_report(now: float, since: float | None = None) -> str:
        """Requests still running, oldest first.

        ``since`` additionally folds in requests that *finished* after that
        moment, tagged ``done``. The watchdog passes the start of the window it
        just lost to a stall, because the request responsible has necessarily
        completed before the watchdog could run again.
        """
        entries = [(label, now - started, "") for label, started in in_flight.values()]
        if since is not None:
            entries += [
                (label, ended - started, ", done")
                for label, started, ended in recently_done
                if ended >= since
            ]
        if not entries:
            return "none"
        entries.sort(key=lambda e: e[1], reverse=True)
        shown = entries[:_IN_FLIGHT_REPORT_MAX]
        report = ", ".join(f"{label} ({age:.1f}s{tag})" for label, age, tag in shown)
        extra = len(entries) - len(shown)
        return f"{report} (+{extra} more)" if extra else report

    def _thread_stats() -> tuple[int | None, int | None]:
        """``(borrowed, total)`` AnyIO worker threads, or ``(None, None)``.

        Saturation here is the difference between "one endpoint is slow" and
        "the whole API is wedged": the auth middleware needs a worker thread for
        every single request, so an exhausted pool queues even a static
        ``index.html``.
        """
        try:
            import anyio.to_thread

            stats = anyio.to_thread.current_default_thread_limiter().statistics()
            return stats.borrowed_tokens, int(stats.total_tokens)
        except Exception:  # noqa: BLE001 - diagnostics must never break serving
            return None, None

    async def _watchdog() -> None:
        last_saturation_log = 0.0
        while True:
            before = time.monotonic()
            await asyncio.sleep(_WATCHDOG_TICK)
            now = time.monotonic()
            lag = now - before - _WATCHDOG_TICK
            borrowed, total = _thread_stats()
            if lag >= _WATCHDOG_STALL:
                # The loop could not run during the stall, so the thread counts
                # are a post-drain sample — the lag itself is the finding.
                # `since=before` is what surfaces the culprit: whatever blocked
                # the loop had to finish before this line could be written, so
                # it is in `recently_done`, not in flight.
                log.warning(
                    "event loop stalled for %.1fs (threads %s/%s after; %s); "
                    "during stall: %s",
                    lag,
                    borrowed,
                    total,
                    _pressure_report(),
                    _in_flight_report(now, since=before),
                )
            elif (
                borrowed is not None
                and total
                and borrowed >= total
                and now - last_saturation_log >= _SATURATION_LOG_INTERVAL
            ):
                last_saturation_log = now
                log.warning(
                    "AnyIO worker pool saturated (%s/%s threads; %s); in-flight: %s",
                    borrowed,
                    total,
                    _pressure_report(),
                    _in_flight_report(now),
                )

    # Saved cron schedules. Its own thread (a tick opens SQLite and can block on
    # the job-manager lock — neither belongs on the event loop), started with the
    # app so a container that only runs `synopticon web` still gets recurring
    # jobs without a cron daemon inside the image.
    from .scheduler import Scheduler

    scheduler = Scheduler(lambda: store.connect(settings), jm)

    @asynccontextmanager
    async def lifespan(app):
        watchdog = asyncio.create_task(_watchdog())
        scheduler.start()
        try:
            yield
        finally:
            scheduler.stop()
            watchdog.cancel()
            jm.shutdown()
            nas_session = getattr(app.state, "nas_session", None)
            if nas_session is not None:
                nas_session.reset()

    app = FastAPI(title="Synopticon", lifespan=lifespan)
    # `app` is bound here, 121 lines after `limiter`/`allowlist`/`trusted_proxies`
    # are constructed above -- this publication cannot be folded back into that
    # construction (D9a). It is the only handle route 6 (api_change_password)
    # and routes 19-20 have on the limiter.
    app.state.login_limiter = limiter
    app.state.ip_allowlist = allowlist
    app.state.proxy_trust = trusted_proxies
    _add_gzip(app)
    # /assets public (hashed SPA bundle, no user data); mount only if the built
    # dist exists so create_app works without a build. /crops is guarded by auth.
    assets_dir = dist / "assets"
    if assets_dir.is_dir():
        app.mount(
            "/assets",
            _CachedStatic(
                directory=str(assets_dir), cache_control=_ASSETS_CACHE_CONTROL
            ),
            name="assets",
        )
    app.mount(
        "/crops",
        _CachedStatic(directory=str(crops_dir), cache_control=_CROPS_CACHE_CONTROL),
        name="crops",
    )

    def conn() -> Connection:
        return store.connect(settings)

    # Whole-library review lookups (crops / hidden persons / person->faces),
    # cached on a DB fingerprint. See review/lookups.py for why this is not a TTL.
    lookups = LookupCache()

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
    # Session cookie -> (ident, monotonic deadline). A review grid issues one
    # request per crop, and resolving the cookie from SQLite each time means a
    # connection + query + occasional last_seen write per image. The key now
    # folds in the client facts (see `_credential`), which is the only thing
    # standing between a stolen cookie and a 30 s pin-replay window -- so the
    # cache must evict rather than grow without bound: the insert site below
    # drops expired entries, then pops the earliest deadline until the cache is
    # back under `_AUTH_CACHE_MAX`. It is NEVER `clear()`d wholesale except by
    # the no-argument revocation call -- a wholesale flush under attack would
    # convert a memory-growth problem into evicting every signed-in user's
    # verdict at once.
    auth_cache: dict[str, tuple[Any, float]] = {}
    auth_cache_lock = threading.Lock()

    def _invalidate_auth_cache(
        credential: str | None = None, *, prefix: str | None = None
    ) -> None:
        """Drop one cached verdict, every verdict for one session token, or all
        of them.

        ``prefix`` drops every entry whose key starts with it -- a session's
        `cache_key` is `"s:<sha256(token)>:<sha256(facts)>"`, one row per
        distinct client that has used the cookie, so a revocation has to sweep
        by `cache_prefix(token)` rather than pop a single key. With neither
        argument, every entry is cleared (used by `configio._drop_cached_auth`
        and `security_routes._drop_cached_auth`, which have no single
        credential to name).
        """
        with auth_cache_lock:
            if prefix is not None:
                for key in [k for k in auth_cache if k.startswith(prefix)]:
                    auth_cache.pop(key, None)
                return
            if credential is None:
                auth_cache.clear()
            else:
                auth_cache.pop(credential, None)

    def _authenticate(request: Request, c: Connection) -> tuple[Any, str | None]:
        """Return ``(ident, reason)``.

        ``ident`` is ``("user", id)`` / ``("apikey", id)`` or ``None``.
        ``reason`` is ``None`` except for a pin violation, which returns
        ``(None, "pin")`` — the unauthenticated branch downstream tells that
        case apart from an ordinary missing/expired credential so it can answer
        with the pin-specific message and drop the browser's now-invalid
        cookie.
        """
        header = request.headers.get("authorization", "")
        if header.startswith("Bearer "):
            kid = auth.validate_api_key(c, header[len("Bearer ") :].strip())
            if kid is not None:
                return ("apikey", kid), None
            return None, None
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            client = clientip.client_facts(request)
            try:
                uid = auth.validate_session(c, token, client=client)
            except auth.SessionPinViolation as exc:
                # Never the stored hash -- only the facts THIS request
                # presented, which is what the operator needs to recognise
                # "new phone" versus "someone else". `repr()` neutralises a
                # newline in the User-Agent before it reaches uvicorn's plain
                # StreamHandler.
                log.warning(
                    "session pin violation: ip=%s ua=%s destroyed=%s",
                    clientip.client_ip(request),
                    repr(client.user_agent[: clientip.UA_MAX]),
                    exc.destroyed,
                )
                return None, "pin"
            if uid is not None:
                return ("user", uid), None
        return None, None

    def _credential(request: Request) -> str | None:
        """The auth-cache key to cache this request's verdict under, if any.

        Session cookies only. The burst this cache exists for is a browser
        fetching a page's worth of crops, and a browser never sends a Bearer
        header — so API keys skip the cache entirely and keep exact revocation
        semantics (revoke it in the DB by any means and the next call is 401).

        The key includes the client facts (``sessions.cache_key``), so a
        cached verdict can never be replayed by a different client within the
        TTL — the whole point of session pinning would otherwise be defeated by
        timing alone.
        """
        if request.headers.get("authorization", "").startswith("Bearer "):
            return None
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            return None
        return auth.cache_key(token, clientip.client_facts(request))

    def _anonymous(request: Request) -> bool:
        """True when the request presents no credential of any kind.

        ``_credential`` collapses "has a Bearer header" and "has nothing" to the
        same ``None`` (both are uncacheable), which is the wrong distinction
        here — an anonymous request needs no database at all.
        """
        return not request.headers.get(
            "authorization", ""
        ).startswith("Bearer ") and not request.cookies.get(SESSION_COOKIE)

    # Once an account exists it can never be removed (there is no delete-user
    # route), so first boot is a one-way latch: cache it and stop querying.
    have_users = False

    def _auth_lookup(request: Request) -> tuple[Any, bool, str | None]:
        """Blocking auth resolution: ``(ident, first_boot, reason)``.

        Runs in the threadpool — never on the event loop. Opening a SQLite
        connection and reading from it can block for as long as the DB lock is
        held, and doing that on the loop stalls every other in-flight request
        with it (a single slow query becomes a server-wide stall).

        ``reason`` is ``"pin"`` when a live session was presented by a client
        it was not pinned to (section 6 step 21); ``None`` otherwise.
        """
        nonlocal have_users
        credential = _credential(request)
        if credential is not None and have_users:
            now = time.monotonic()
            with auth_cache_lock:
                hit = auth_cache.get(credential)
                if hit is not None and hit[1] > now:
                    return hit[0], False, None
        # No credential at all, and we already know an account exists: the
        # verdict is "anonymous" without asking the database. This is the whole
        # of the healthcheck path (a loopback poller has no cookie) and of every
        # 401, both of which otherwise opened a connection to learn nothing.
        if have_users and _anonymous(request):
            return None, False, None
        header = request.headers.get("authorization", "")
        c = conn()
        try:
            if not have_users:
                have_users = auth.has_users(c)
            ident, reason = _authenticate(request, c)
            if ident is None and reason is None and header.startswith("Bearer "):
                # A rejected access key. Never on a successful key (that would
                # turn a polling script into thousands of rows a day) and only
                # when the operator has not turned this switch off; bounded to
                # one row per address prefix per minute so a looping client
                # with a stale key cannot flood the log.
                if settings.security.log_failed_api_keys:
                    ip = clientip.client_ip(request)
                    if auth.authlog.blocked_log.allow(f"apikey:{clientip.ip_prefix(ip)}"):
                        auth.record_attempt(
                            c,
                            event="api_key",
                            outcome="failure",
                            reason="unknown_or_revoked_key",
                            ip=ip,
                            user_agent=clientip.user_agent(request),
                        )
            if reason == "pin":
                token = request.cookies.get(SESSION_COOKIE)
                if token:
                    # The legitimate browser must not keep working off the
                    # cache for up to _AUTH_CACHE_TTL after this session was
                    # destroyed (or, for an unpinnable client=None violation,
                    # while its facts keep disagreeing every request).
                    _invalidate_auth_cache(prefix=auth.cache_prefix(token))
        finally:
            c.close()
        if credential is not None and have_users and ident is not None:
            with auth_cache_lock:
                now = time.monotonic()
                deadline = now + _AUTH_CACHE_TTL
                for key in [k for k, (_, dl) in auth_cache.items() if dl <= now]:
                    auth_cache.pop(key, None)
                auth_cache[credential] = (ident, deadline)
                while len(auth_cache) > _AUTH_CACHE_MAX:
                    oldest = min(auth_cache, key=lambda k: auth_cache[k][1])
                    auth_cache.pop(oldest, None)
        return ident, not have_users, reason

    # -- middleware --------------------------------------------------------- #
    # All three layers below are *pure ASGI*, not Starlette's BaseHTTPMiddleware
    # (`@app.middleware("http")`), and must stay that way. BaseHTTPMiddleware
    # relays the response through a memory object stream inside a task group,
    # which for a `StreamingResponse` means every SSE event crosses one such
    # stream per layer; with three layers a job log's events were being pumped
    # through nine hops on the event loop. Worse, when the client disconnects
    # mid-stream the inner app finishes without ever sending a response start,
    # and BaseHTTPMiddleware turns that into a spurious, un-catchable
    # `RuntimeError: No response returned.` — logged on every abandoned job
    # stream and on every shutdown with a stream open. Pure ASGI has neither
    # problem: `send` is passed straight through.

    def _defaulting_send(send, headers: list[tuple[bytes, bytes]]):
        """Wrap ``send`` so ``headers`` are added if not already present.

        Matches the ``response.headers.setdefault`` the dispatch-style
        middleware used to do — a handler that sets its own value keeps it.
        """

        async def wrapped(message):
            if message["type"] == "http.response.start":
                raw = list(message.get("headers") or [])
                present = {name.lower() for name, _ in raw}
                raw.extend((n, v) for n, v in headers if n not in present)
                message = {**message, "headers": raw}
            await send(message)

        return wrapped

    class _AuthMiddleware:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http":
                await self.app(scope, receive, send)
                return

            request = Request(scope, receive)
            path = scope["path"]
            method = scope["method"]
            is_api = path.startswith("/api/")

            async def forward(response=None):
                if response is not None:
                    await response(scope, receive, send)
                else:
                    await self.app(scope, receive, send)

            # Liveness probe: answered before anything that can block. It takes
            # no worker thread, opens no database connection and stats no file,
            # so the only thing that can fail it is a dead event loop — which is
            # precisely what a container healthcheck should be asking about.
            #
            # It must never go through the auth lookup below. That hop needs an
            # AnyIO worker thread, and during a long `extract` the pool is the
            # scarce resource: the job's crop writes and per-photo commits hit
            # the same (often NFS-backed) volume the server reads, every
            # DB-touching handler parks in D-state, and a probe queued behind 40
            # of those blows its timeout. Three of those in a row and the
            # orchestrator restarts the container out from under the job.
            if path == "/api/health":
                await forward()
                return

            # Network allowlist. Placed here -- after the health short-circuit,
            # before the /assets/ bypass and everything else -- because both of
            # those `forward()` ahead of every auth decision, and a gate placed
            # any later would silently exempt the whole SPA bundle from a
            # feature whose help text promises it covers "the app bundle" and
            # refuses an address "before it can even see the sign-in page". No
            # connection is opened, no worker thread is taken, no scrypt runs,
            # and nothing is written to web_auth_log -- a per-denied-request DB
            # write would turn the gate into a write amplifier for exactly the
            # traffic it exists to reject. Loopback is allowed unconditionally
            # (clientip RULE 4); the throttle below still throttles it like any
            # other address -- the two features make opposite trades on the
            # same address on purpose.
            if allowlist.active and not allowlist.allows(clientip.client_ip(request)):
                ip = clientip.client_ip(request)
                now = time.monotonic()
                last = _denied_log_seen.get(ip)
                if last is None or now - last >= _DENIED_LOG_INTERVAL:
                    if len(_denied_log_seen) > 1024:
                        _denied_log_seen.clear()
                    _denied_log_seen[ip] = now
                    log.warning("refused by network allowlist: %s", ip)
                if is_api:
                    denial = JSONResponse(
                        {
                            "error": "This address is not allowed to reach Synopticon.",
                            "client_ip": ip,
                            "blocked": True,
                        },
                        status_code=403,
                    )
                else:
                    denial = HTMLResponse(
                        _DENIED_HTML.format(ip=html.escape(ip)), status_code=403
                    )
                denial.headers["Cache-Control"] = "no-store"
                await forward(denial)
                return

            # Hashed SPA bundle: public so /login and /setup can load before a
            # session exists (mirrors the old /static bypass).
            if path.startswith("/assets/"):
                await forward()
                return
            # Dist-root files (favicons, site.webmanifest, img/…) are public too
            # — the login view needs them unauthenticated. index.html is NOT
            # bypassed: it follows the page auth rules below (only /login and
            # /setup unauth'd). /api and /crops paths can never name a dist
            # file, so skip the stat() for them — /crops in particular is one
            # request per face crop.
            if (
                not is_api
                and not path.startswith("/crops/")
                and path != "/index.html"
                and _resolve_dist_file(path) is not None
            ):
                await self.app(
                    scope,
                    receive,
                    _defaulting_send(
                        send,
                        [(b"cache-control", _DIST_FILE_CACHE_CONTROL.encode())],
                    ),
                )
                return

            # Anonymous, and an account is already known to exist: the verdict is
            # "not authenticated" without asking the database, so don't spend a
            # worker thread to reach it. Same short-circuit `_auth_lookup` makes
            # internally, hoisted onto the loop — it is pure header inspection,
            # and it keeps every 401 independent of a threadpool a running job
            # can saturate.
            if have_users and _anonymous(request):
                ident, first_boot, auth_reason = None, False, None
            else:
                ident, first_boot, auth_reason = await run_in_threadpool(
                    _auth_lookup, request
                )
            # Written into the shared scope, which is the same dict the route
            # handler's own Request wraps — `request.state.ident` downstream.
            state = scope.setdefault("state", {})
            state["ident"] = ident
            state["first_boot"] = first_boot
            state["auth_reason"] = auth_reason

            # CSRF: mutating API calls must be JSON (cookie is SameSite=Lax; a
            # cross-site form post cannot set application/json).
            if is_api and method in _MUTATING:
                ctype = request.headers.get("content-type", "").split(";")[0].strip()
                if ctype != "application/json":
                    await forward(
                        JSONResponse(
                            {"error": "Content-Type must be application/json"},
                            status_code=415,
                        )
                    )
                    return

            if first_boot:
                allowed = (
                    path == "/setup"
                    or path.startswith("/api/setup")
                    or path == "/api/auth/create-account"
                    # /api/auth/me must answer during first boot so the SPA
                    # router guard can detect the claim flow (first_boot: true).
                    or path == "/api/auth/me"
                )
                if allowed:
                    await forward()
                elif is_api:
                    await forward(
                        JSONResponse(
                            {"error": "setup required", "setup": True}, status_code=403
                        )
                    )
                else:
                    await forward(RedirectResponse("/setup", status_code=302))
                return

            # Users exist. /login serves the SPA shell without a session so the
            # LoginView can render (its client-side guard handles the rest).
            # /api/auth/me reports auth state (always 200) and the JSON login
            # accepts credentials; both still pass the CSRF gate above.
            if (
                path == "/login"
                or path == "/api/auth/me"
                or (path == "/api/auth/login" and method == "POST")
                or (path == "/api/auth/login/verify" and method == "POST")
            ):
                await forward()
                return
            if path == "/api/auth/create-account":
                await forward(
                    JSONResponse({"error": "account already exists"}, status_code=403)
                )
                return

            if ident is None:
                if auth_reason == "pin":
                    # A live session was presented by a client it was not
                    # pinned to. Different from an ordinary missing/expired
                    # credential: the browser has to be told to drop the
                    # cookie it is holding, not just sign in again.
                    if is_api:
                        denial = JSONResponse(
                            {
                                "error": "signed out — this browser or network changed",
                                "reason": "pin",
                            },
                            status_code=401,
                        )
                    else:
                        denial = RedirectResponse(
                            f"/login?next={quote(path)}&reason=pin", status_code=302
                        )
                    denial.delete_cookie(SESSION_COOKIE, path="/")
                    await forward(denial)
                    return
                if is_api:
                    await forward(
                        JSONResponse(
                            {"error": "authentication required"}, status_code=401
                        )
                    )
                else:
                    await forward(
                        RedirectResponse(f"/login?next={quote(path)}", status_code=302)
                    )
                return
            await forward()

    class _NoStoreAPI:
        """``Cache-Control: no-store`` on every ``/api`` response.

        Wraps the auth layer, so the replies auth short-circuits (401/302/415)
        are covered too. Without it a browser is free to heuristically cache a
        GET /api/… and serve a stale review queue back to the SPA.
        """

        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http" and scope["path"].startswith("/api/"):
                send = _defaulting_send(send, [(b"cache-control", b"no-store")])
            await self.app(scope, receive, send)

    class _RequestTiming:
        """Outermost layer: its clock covers auth, gzip, routing and handler.

        Timing stops at ``http.response.start`` rather than at the end of the
        body, which is what makes the number meaningful for the SSE endpoint —
        a job stream is *supposed* to stay open for the length of the job, and
        measuring that would bury the real stalls under one line per stream.
        The in-flight set it maintains is what makes the watchdog's lines
        actionable: without it a stall says "something took 24 s" and never
        says what else was waiting.
        """

        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http":
                await self.app(scope, receive, send)
                return

            key = next(request_seq)
            label = f"{scope['method']} {scope['path']}"
            started = time.monotonic()
            in_flight[key] = (label, started)
            done = False

            def finish():
                nonlocal done
                if done:
                    return
                done = True
                in_flight.pop(key, None)
                now = time.monotonic()
                elapsed = now - started
                recently_done.append((label, started, now))
                if elapsed < _SLOW_REQUEST:
                    return
                # `route` is set by the router downstream, so it is available by
                # the time the response starts; it falls back to the raw path
                # for a request that never matched anything.
                route = scope.get("route")
                key_path = getattr(route, "path", None) or scope["path"]
                throttle_key = f"{scope['method']} {key_path}"
                seen, suppressed = slow_log_state.get(throttle_key, (0.0, 0))
                if now - seen < _SLOW_REQUEST_LOG_INTERVAL:
                    slow_log_state[throttle_key] = (seen, suppressed + 1)
                    return
                borrowed, total = _thread_stats()
                log.warning(
                    "slow request: %s took %.1fs (threads %s/%s; %s%s); concurrent: %s",
                    label,
                    elapsed,
                    borrowed,
                    total,
                    _pressure_report(),
                    f"; {suppressed} similar suppressed" if suppressed else "",
                    _in_flight_report(now),
                )
                slow_log_state[throttle_key] = (now, 0)

            async def timed_send(message):
                if message["type"] == "http.response.start":
                    finish()
                await send(message)

            try:
                await self.app(scope, receive, timed_send)
            finally:
                # Covers the paths that never reach a response start: an
                # exception on the way down, or a client that vanished.
                finish()

    # Added innermost-first: Starlette wraps in reverse, so the last one added
    # is the outermost. Order is load-bearing — see each class's docstring.
    app.add_middleware(_AuthMiddleware)
    app.add_middleware(_NoStoreAPI)
    app.add_middleware(_RequestTiming)
    # Outermost. Added last, so it wraps the timing layer and everything under
    # it: the auth middleware's allowlist gate, its /api/health short-circuit
    # and every route see scope["synopticon.client"] already attached.
    app.add_middleware(clientip.ProxyHeaders, trusted=trusted_proxies)

    # -- auth helpers ------------------------------------------------------- #
    def _set_session_cookie(resp, token: str, request: Request) -> None:
        """Attach the session cookie (HttpOnly, SameSite=Lax, 30d).

        ``Secure`` follows ``clientip.client_scheme``, not
        ``request.url.scheme``: uvicorn's own proxy-header handling is switched
        off explicitly (``serve()`` passes ``proxy_headers=False``), so the
        effective scheme behind a reverse proxy comes from Synopticon's own
        ``clientip.ProxyHeaders`` and is only trusted from an address listed in
        ``[security] trusted_proxies``. Out of the box (no listed proxy) the
        cookie is ``Secure`` only over a direct https connection.
        """
        resp.set_cookie(
            SESSION_COOKIE,
            token,
            httponly=True,
            samesite="lax",
            secure=clientip.client_scheme(request) == "https",
            max_age=30 * 86400,
            path="/",
        )

    # -- auth: create-account / JSON login / logout / me (SPA) -------------- #
    def _rate_limited(t) -> JSONResponse:
        """The one 429 body used by every throttled route in this file (section
        5.4). The tier that fired is never named -- that would itself be an
        oracle -- and the recovery line always names
        ``max_failures_per_address``, the one config key that can disable the
        address tier, even when it was the pair tier that actually blocked
        (which clears on its own well within a user's patience).
        """
        return JSONResponse(
            {
                "error": f"Too many attempts — try again in {t.retry_after} seconds.",
                "retry_after": t.retry_after,
                "recovery": (
                    "If this is your own address and you cannot get back in, set "
                    "[security] max_failures_per_address = 0 in config.toml and "
                    "restart Synopticon."
                ),
            },
            status_code=429,
            headers={"Retry-After": str(t.retry_after)},
        )

    def _blocked_log_row(event: str, ip: str, ua: str, username: str | None = None) -> None:
        """Record at most one row per address prefix per minute for a blocked
        anonymous attempt. An attacker who ignores 429 keeps sending; without
        this gate a per-attempt connection + INSERT turns the throttle itself
        into a write amplifier for exactly the traffic it exists to reject.
        """
        if auth.authlog.blocked_log.allow(f"blocked:{clientip.ip_prefix(ip)}"):
            c = conn()
            try:
                auth.record_attempt(
                    c,
                    event=event,
                    outcome="blocked",
                    reason="rate_limited",
                    username=username,
                    ip=ip,
                    user_agent=ua,
                )
            finally:
                c.close()

    async def _json_object(request: Request) -> dict | None:
        """The request body as a dict, or None for anything else.

        These three routes sit in the middleware's allowlist, so an anonymous
        client reaches them before any throttle charge. An unguarded parse
        turned `[]`, `"x"` or malformed JSON into an unlogged 500 with a
        traceback per hit -- free noise to bury the sign-in log's warnings in.
        """
        try:
            body = await request.json()
        except Exception:
            return None
        return body if isinstance(body, dict) else None

    @app.post("/api/auth/create-account")
    async def api_create_account(request: Request):
        # Reachable only during first boot (middleware blocks it afterwards).
        body = await _json_object(request)
        if body is None:
            return JSONResponse(
                {"error": "body must be a JSON object"}, status_code=422
            )
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        if not username or not password:
            # A 422 for a missing field must never touch the throttle -- it is
            # a form slip on the one attempt a first-time user gets, and arming
            # a growing lockout on it is a self-inflicted setup failure.
            return JSONResponse(
                {"error": "username and password are required"}, status_code=422
            )

        resolved = clientip.resolved(request)
        t = limiter.verdict(resolved, username, scope="create_account")
        if not t.allowed:
            return _rate_limited(t)

        def work():
            c = conn()
            try:
                if auth.has_users(c):
                    return None
                uid = auth.create_user(c, username, password)
                token = auth.create_session(c, uid, client=clientip.client_facts(request))
                auth.record_attempt(
                    c,
                    event="create_account",
                    outcome="success",
                    username=username,
                    user_id=uid,
                    ip=resolved.ip,
                    user_agent=clientip.user_agent(request),
                )
                return uid, token
            finally:
                c.close()

        # scrypt is deliberately expensive; off the loop it must go.
        try:
            created = await run_in_threadpool(work)
        except auth.UsernameTakenError:
            # The pair tier is armed here, and here alone (F13): with no users
            # yet the middleware already answers 403 for every other post-boot
            # attempt, so this branch needs an actual race between two
            # concurrent first-boot posts to fire outside the shared address
            # window.
            limiter.record_failure(resolved, username, scope="create_account")

            def log_taken():
                c = conn()
                try:
                    auth.record_attempt(
                        c,
                        event="create_account",
                        outcome="failure",
                        reason="username_taken",
                        username=username,
                        ip=resolved.ip,
                        user_agent=clientip.user_agent(request),
                    )
                finally:
                    c.close()

            await run_in_threadpool(log_taken)
            return JSONResponse({"error": "username taken"}, status_code=409)
        if created is None:
            return JSONResponse({"error": "account already exists"}, status_code=403)
        uid, token = created
        limiter.record_success(resolved, username, scope="create_account")
        resp = JSONResponse({"ok": True, "user_id": uid}, status_code=201)
        _set_session_cookie(resp, token, request)
        return resp

    # -- auth: JSON login / logout / me (SPA) ------------------------------- #
    @app.post("/api/auth/login")
    async def api_login(request: Request):
        body = await _json_object(request)
        if body is None:
            return JSONResponse(
                {"error": "body must be a JSON object"}, status_code=422
            )
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        resolved = clientip.resolved(request)
        ua = clientip.user_agent(request)

        # Throttle verdict from in-memory state, on the loop, before any
        # connection or worker thread (D9c) -- both tiers are always armed, on
        # the one resolved address, with no trust parameter of any kind.
        t = limiter.verdict(resolved, username)
        if not t.allowed:
            await run_in_threadpool(
                _blocked_log_row, "login", resolved.ip, ua, username
            )
            return _rate_limited(t)

        def work():
            c = conn()
            try:
                uid = auth.verify_password(c, username, password)
                needs_code = auth.challenge_required(c, username, uid)
                if needs_code:
                    token = auth.start_login_challenge(
                        c, username, uid,
                        ttl_seconds=settings.security.login_challenge_ttl,
                    )
                    auth.record_attempt(
                        c,
                        event="login",
                        outcome="pending",
                        reason="password_ok" if uid else "password_bad",
                        username=username,
                        user_id=uid,
                        ip=resolved.ip,
                        user_agent=ua,
                    )
                    return ("challenge", token, settings.security.login_challenge_ttl)
                if uid is None:
                    auth.record_attempt(
                        c,
                        event="login",
                        outcome="failure",
                        reason=(
                            "bad_credentials"
                            if (username and password)
                            else "missing_credentials"
                        ),
                        username=username,
                        ip=resolved.ip,
                        user_agent=ua,
                    )
                    return None
                token = auth.create_session(c, uid, client=clientip.client_facts(request))
                auth.record_attempt(
                    c,
                    event="login",
                    outcome="success",
                    username=username,
                    user_id=uid,
                    ip=resolved.ip,
                    user_agent=ua,
                )
                return ("session", token)
            finally:
                c.close()

        # verify_password runs scrypt (n=2**14) on both the hit and the miss
        # path — ~100 ms of pure CPU. On the event loop that stalls every other
        # in-flight request, so a login freezes the whole GUI.
        result = await run_in_threadpool(work)
        if result is None:
            limiter.record_failure(resolved, username)
            return JSONResponse(
                {"error": "Invalid username or password."}, status_code=401
            )
        if result[0] == "challenge":
            _, challenge, ttl = result
            # Feeds the pair *attempt* window (never the exponential backoff)
            # and the address window -- this is what still bounds guessing once
            # anyone on the instance is enrolled, because challenge_required
            # sends every wrong password down this branch and record_failure is
            # never reached here at all.
            limiter.record_pending(resolved, username)
            return JSONResponse(
                {"mfa_required": True, "challenge": challenge, "expires_in": ttl}
            )
        _, token = result
        limiter.record_success(resolved, username)
        resp = JSONResponse({"ok": True, "username": username})
        _set_session_cookie(resp, token, request)
        return resp

    @app.post("/api/auth/login/verify")
    async def api_login_verify(request: Request):
        body = await _json_object(request)
        if body is None:
            return JSONResponse(
                {"error": "body must be a JSON object"}, status_code=422
            )
        challenge = (body.get("challenge") or "").strip()
        code = (body.get("code") or "").strip()
        resolved = clientip.resolved(request)
        ua = clientip.user_agent(request)

        def restart() -> JSONResponse:
            return JSONResponse(
                {"error": "Sign in timed out — start again.", "restart": True},
                status_code=401,
            )

        # Step 11: the address tier ALONE, before hop A -- this route needs no
        # session and sits in the users-exist allowlist beside /api/auth/login,
        # so without this a client posting random challenge strings buys a
        # connection and a worker thread per request with no charge at all.
        t0 = limiter.verdict_address(resolved, scope="totp")
        if not t0.allowed:
            await run_in_threadpool(_blocked_log_row, "login_code", resolved.ip, ua)
            return _rate_limited(t0)

        def peek():
            c = conn()
            try:
                taken = auth.peek_login_challenge(
                    c, challenge, max_attempts=settings.security.login_code_attempts
                )
                if taken is None:
                    auth.record_attempt(
                        c,
                        event="login_code",
                        outcome="failure",
                        reason="unknown_challenge",
                        username=None,
                        ip=resolved.ip,
                        user_agent=ua,
                    )
                return taken
            finally:
                c.close()

        # Hop A: non-consuming peek, so a blocked replay of a stolen challenge
        # cannot burn the real user's remaining code attempts.
        taken = await run_in_threadpool(peek)
        if taken is None:
            limiter.record_address(resolved, scope="totp")
            return restart()
        username, _peeked_uid = taken

        # Step 13: the full pair verdict, now that hop A named the account.
        t1 = limiter.verdict(resolved, username, scope="totp")
        if not t1.allowed:
            await run_in_threadpool(
                _blocked_log_row, "login_code", resolved.ip, ua, username
            )
            return _rate_limited(t1)

        def judge():
            c = conn()
            try:
                row = auth.take_login_challenge(
                    c, challenge, max_attempts=settings.security.login_code_attempts
                )
                if row is None:
                    return ("restart",)
                row_username, uid = row
                accepted = False
                if uid is not None:
                    if auth.verify_totp(
                        c, uid, code, skew=settings.security.totp_skew_steps
                    ):
                        accepted = True
                    elif auth.consume_recovery_code(c, uid, code):
                        accepted = True
                if not accepted:
                    limiter.record_failure(resolved, row_username, scope="totp")
                    # Unconditionally, and never behind `if uid is None`: the
                    # password scope is what /api/auth/login reads, so arming it
                    # only for a wrong password makes the next login's 429 a
                    # read-out of whether the password was right -- a three
                    # request password oracle against the very accounts a
                    # second factor is supposed to protect.
                    limiter.record_failure(resolved, row_username, scope="password")
                    auth.record_attempt(
                        c,
                        event="login_code",
                        outcome="failure",
                        reason="bad_code" if uid else "bad_password",
                        username=row_username,
                        user_id=uid,
                        ip=resolved.ip,
                        user_agent=ua,
                    )
                    return ("rejected",)
                token = auth.create_session(c, uid, client=clientip.client_facts(request))
                auth.delete_login_challenge(c, challenge)
                recovery_remaining = auth.count_recovery_codes(c, uid)
                auth.record_attempt(
                    c,
                    event="login_code",
                    outcome="success",
                    username=row_username,
                    user_id=uid,
                    ip=resolved.ip,
                    user_agent=ua,
                )
                limiter.record_success(resolved, row_username, scope="totp")
                limiter.record_success(resolved, row_username)
                return ("accepted", row_username, token, recovery_remaining)
            finally:
                c.close()

        outcome = await run_in_threadpool(judge)
        if outcome[0] == "restart":
            return restart()
        if outcome[0] == "rejected":
            return JSONResponse({"error": "That code was not accepted."}, status_code=401)
        _, accepted_username, token, recovery_remaining = outcome
        resp = JSONResponse(
            {
                "ok": True,
                "username": accepted_username,
                "recovery_remaining": recovery_remaining,
            }
        )
        _set_session_cookie(resp, token, request)
        return resp

    @app.post("/api/auth/logout")
    def api_logout(request: Request):
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            c = conn()
            try:
                client = clientip.client_facts(request)
                try:
                    uid = auth.validate_session(c, token, client=client)
                except auth.SessionPinViolation:
                    uid = None
                username = auth.username_for(c, uid) if uid is not None else None
                auth.delete_session(c, token)
                auth.record_attempt(
                    c,
                    event="logout",
                    outcome="success",
                    username=username,
                    user_id=uid,
                    ip=clientip.client_ip(request),
                    user_agent=clientip.user_agent(request),
                )
            finally:
                c.close()
            # The middleware trusts a validated cookie for _AUTH_CACHE_TTL; a
            # logout must take effect on the very next request. The cache key
            # now folds in the client facts, so one token can own several
            # entries -- the sweep is by prefix, not by a single key.
            _invalidate_auth_cache(prefix=auth.cache_prefix(token))
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(SESSION_COOKIE, path="/")
        return resp

    @app.get("/api/health")
    async def api_health():
        """Container liveness probe. Deliberately does nothing.

        `async def`, not `def`: a sync handler is dispatched to the AnyIO worker
        pool, which is exactly the resource a long-running job starves. Keep it
        free of every kind of I/O — no database, no filesystem, no NAS — so that
        a slow disk or a locked DB can never be mistaken for a dead server and
        get the container restarted mid-job. Use `/api/stats` for readiness.
        """
        return {"ok": True, "version": app_version}

    @app.get("/api/auth/me")
    def api_me(request: Request):
        """Auth state for the SPA router guard. Always 200 (even unauthenticated
        and during first boot) — allowlisted in both middleware branches.

        `totp_enabled` and `session_pinning` describe THE CALLER'S OWN account
        and are `None` whenever `ident` is `None` or not a user session -- this
        route answers 200 to anybody, and on a single-admin instance "nobody
        here has a second factor" would be a direct invitation.
        """
        first_boot = bool(getattr(request.state, "first_boot", False))
        ident = getattr(request.state, "ident", None)
        username = None
        totp_enabled = None
        session_pinning = None
        authenticated = ident is not None
        if ident and ident[0] == "user":
            c = conn()
            try:
                username = auth.username_for(c, ident[1])
                totp_enabled = auth.totp_enabled(c, ident[1])
                session_pinning = auth.get_pin_mode(c, ident[1])
            finally:
                c.close()
        return {
            "authenticated": authenticated,
            "username": username,
            "first_boot": first_boot,
            "version": app_version,
            "totp_enabled": totp_enabled,
            "session_pinning": session_pinning,
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

    @app.get("/api/about")
    def api_about():
        """Build + environment facts for the About page (and bug reports).

        Deliberately import-light: package versions come from
        ``importlib.metadata`` (which reads dist metadata, not the modules), the
        pipeline version from the memoized manifest helper — never
        ``pipeline.runner``. See the responsiveness invariants.
        """
        import platform
        import sys
        from importlib.metadata import PackageNotFoundError
        from importlib.metadata import version as dist_version

        from ..cpu import available_cores, cgroup_cpu_limit, physical_cores
        from .stats import _pipeline_version_cached

        def dist(name: str) -> str | None:
            try:
                return dist_version(name)
            except PackageNotFoundError:
                return None
            except Exception:  # noqa: BLE001 - metadata must never 500 the page
                return None

        models_ready, pv = _pipeline_version_cached(settings)
        quota = cgroup_cpu_limit()
        return {
            "version": app_version,
            "repo_url": "https://github.com/fdebijl/synopticon",
            "pipeline_version": pv,
            "models_ready": models_ready,
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "cpu": {
                "available_cores": available_cores(),
                "physical_cores": physical_cores(),
                "cgroup_quota": quota,
            },
            "paths": {
                "data_dir": str(settings.storage.data_dir),
                "models_dir": str(settings.storage.models_dir),
                "db_path": str(settings.storage.db_path),
            },
            # Never the DSN: it carries the password. Host/database only.
            "database": {
                "backend": settings.database.backend,
                "target": (
                    str(settings.storage.db_path)
                    if settings.database.backend == "sqlite"
                    else f"{settings.database.host}:{settings.database.port}/{settings.database.database}"
                ),
            },
            "packages": {
                name: dist(name)
                for name in (
                    "onnxruntime",
                    "onnxruntime-gpu",
                    "numpy",
                    "fastapi",
                    "faiss-cpu",
                    "torch",
                )
            },
        }

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
            # submit() takes the manager lock and writes job.json to disk.
            job_id = await run_in_threadpool(
                lambda: jm.submit(
                    name, params, confirm=confirm, confirm_phrase=confirm_phrase
                )
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
        # Threadpooled: for a job this process never ran, `get` reads job.json and
        # replays events.jsonl/stdout/stderr off disk. Doing that here also warms
        # the replay cache, so the async generator below only ever touches memory.
        if await run_in_threadpool(lambda: jm.get(job_id)) is None:
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
            # Without the shared cache these three lookups are rebuilt from the
            # whole library on every scroll page — O(all faces) work for an
            # O(page) response.
            lk = lookups.get(c, settings)
            items = queries.load_review_items(
                c,
                settings,
                kind=kind,
                status=status,
                limit=limit,
                offset=offset,
                crops=lk.crops,
                hidden=lk.hidden,
                person_face_map=lk.person_face_map,
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

        def work():
            c = conn()
            try:
                if decision == "undo":
                    return queries.undo_decision(c, item_id)
                return queries.decide_item(c, item_id, decision)
            finally:
                c.close()

        # A SQLite write can block on the DB lock for as long as a job holds it;
        # on the loop that would stall every other request. Review decisions are
        # one-per-keystroke, so this is the hottest write path in the GUI.
        new_status = await run_in_threadpool(work)
        if decision == "undo":
            if new_status is None:
                return JSONResponse(
                    {
                        "error": "cannot undo: item is not in an "
                        "approved/rejected state"
                    },
                    status_code=409,
                )
            return {"item_id": item_id, "status": new_status}
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

        def work():
            c = conn()
            try:
                return queries.bulk_approve(c, kind, min_confidence=min_conf)
            finally:
                c.close()

        return {"approved": await run_in_threadpool(work)}

    @app.get("/api/review/persons")
    def api_review_persons(q: str = "", space: str = "", limit: int = 10):
        if not q.strip():
            return {"persons": []}
        c = conn()
        try:
            lk = lookups.get(c, settings)
            return {
                "persons": queries.person_search(
                    c,
                    q,
                    space=space,
                    limit=limit,
                    crops=lk.crops,
                    person_face_map=lk.person_face_map,
                    hidden=lk.hidden,
                )
            }
        finally:
            c.close()

    @app.post("/api/review/{item_id}/retarget")
    async def api_review_retarget(request: Request, item_id: int):
        body = await request.json()
        space = (body.get("space") or "").strip()
        person_id = body.get("person_id")
        person_name = (body.get("person_name") or "").strip()
        if not space or not isinstance(person_id, int):
            return JSONResponse(
                {"error": "space (str) and person_id (int) are required"},
                status_code=422,
            )

        def work():
            c = conn()
            try:
                return queries.retarget_item(
                    c, item_id, space, person_id, person_name
                )
            finally:
                c.close()

        try:
            result = await run_in_threadpool(work)
        except queries.SpaceMismatch as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        if result is None:
            return JSONResponse(
                {"error": "this item has no target person to change"},
                status_code=400,
            )
        return {"item_id": item_id, **result}

    @app.post("/api/review/{item_id}/name")
    async def api_review_name(request: Request, item_id: int):
        body = await request.json()
        name = (body.get("name") or "").strip()

        def work():
            c = conn()
            try:
                return queries.set_suggested_name(c, item_id, name)
            finally:
                c.close()

        ok = await run_in_threadpool(work)
        if not ok:
            return JSONResponse({"error": "not a new_person item"}, status_code=400)
        return {"item_id": item_id, "suggested_name": name}

    from .ops_routes import register_ops_routes

    register_ops_routes(app, settings, conn)

    from .setup_routes import register_setup_routes

    register_setup_routes(app, settings=settings, conn=conn, auth=auth)

    from .configio import register_config_routes

    register_config_routes(app, settings, conn, jm)

    from .security_routes import register_security_routes

    register_security_routes(app, settings, conn, limiter, allowlist, trusted_proxies)

    from .quickmerger import register_quickmerger_routes

    register_quickmerger_routes(app, settings, conn)

    from .inspect_routes import register_inspect_routes

    # After QuickMerger: Inspect's image proxy reuses the NAS session it owns.
    register_inspect_routes(app, settings, conn, app.state.nas_session)

    from .schedule_routes import register_schedule_routes

    register_schedule_routes(app, settings, conn, jm, scheduler)

    from .backup_routes import register_backup_routes

    register_backup_routes(app, settings, conn)

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
            return FileResponse(
                real, headers={"Cache-Control": _DIST_FILE_CACHE_CONTROL}
            )
        index = dist / "index.html"
        if index.is_file():
            # The shell names the content-hashed bundle, so it must always be
            # revalidated — ETag still makes that a 304 in the common case.
            return FileResponse(index, headers={"Cache-Control": _SHELL_CACHE_CONTROL})
        return JSONResponse(
            {
                "error": "frontend not built",
                "hint": "cd frontend && npm ci && npm run build",
            },
            status_code=503,
        )

    app.state.job_manager = jm
    app.state.scheduler = scheduler
    # Exposed so the routes that revoke a credential (API-key revoke, password
    # change) can drop the middleware's cached verdict immediately instead of
    # leaving it valid for the rest of the TTL.
    app.state.invalidate_auth_cache = _invalidate_auth_cache
    return app


def serve(settings: Settings, host: str = "127.0.0.1", port: int = 8686) -> None:
    """Run the web GUI. Requires the [review] extra.

    Proxy headers are honoured by Synopticon's own ``ProxyHeaders`` middleware,
    and only from the addresses in ``[security] trusted_proxies`` — which is
    empty by default, so out of the box no header is believed and the socket
    peer is used. uvicorn's own ``--proxy-headers`` handling is switched off
    explicitly (it defaults to on, trusting loopback, and this deployment
    terminates TLS on loopback). The session cookie's ``Secure`` flag follows
    ``clientip.client_scheme``, not ``request.url.scheme``, so it only becomes
    ``Secure`` once the proxy is listed.
    """
    _require_fastapi()
    import uvicorn
    from uvicorn.config import LOGGING_CONFIG

    _check_dist_built(_DIST_DIR)

    # `create_app` runs *before* uvicorn binds the socket, and it touches the
    # data volume: crops mkdir, a scan of the jobs dir, DB connect + migrations.
    # On a slow or wedged /data that window is unbounded, and from the outside it
    # is indistinguishable from a crash — the healthcheck gets a flat
    # `connection refused` and the container log is empty, because uvicorn has
    # not printed its own banner yet. Bracket it on stderr (uvicorn's logging is
    # not configured until `run` below, so this is a plain write, not a logger).
    print(
        f"synopticon web: initialising (data_dir={settings.storage.data_dir})",
        file=sys.stderr,
        flush=True,
    )
    _t0 = time.monotonic()
    app = create_app(settings)
    # `create_app` does not know the bind host, so warning 3 below and route
    # 17's diagnostics read it from here.
    app.state.bind_host = host
    print(
        f"synopticon web: initialised in {time.monotonic() - _t0:.1f}s, "
        f"binding {host}:{port}",
        file=sys.stderr,
        flush=True,
    )
    if clientip.is_loopback(host) and not settings.security.trusted_proxies:
        log.warning(
            "synopticon web is bound to 127.0.0.1, so it is reachable only from this "
            "machine or through a proxy running on it, and [security] trusted_proxies is "
            "empty. If a proxy is in front, every visitor arrives as 127.0.0.1: an address "
            "list cannot restrict them and they all share one per-address sign-in budget. "
            "List the proxy in [security] trusted_proxies and configure it to overwrite "
            "X-Forwarded-For (nginx: proxy_set_header X-Forwarded-For $remote_addr;)."
        )
    # The watchdog's findings are only useful if they reach the container log, so
    # route ``synopticon.web`` through uvicorn's own stderr handler rather than
    # leaving it to ``logging.lastResort`` (unformatted, and easy to mistake for
    # stray output).
    log_config = dict(LOGGING_CONFIG)
    log_config["loggers"] = dict(log_config["loggers"])
    log_config["loggers"]["synopticon.web"] = {
        "handlers": ["default"],
        "level": "INFO",
        "propagate": False,
    }
    uvicorn.run(
        app,
        host=host,
        port=port,
        # Explicit, never omitted. uvicorn defaults proxy_headers to True with
        # forwarded_allow_ips = $FORWARDED_ALLOW_IPS or "127.0.0.1", which would
        # install a second, env-settable trust list in front of
        # clientip.ProxyHeaders, rewrite scope["client"] before we ever see it,
        # and on the documented loopback+nginx topology it would fire on every
        # request.
        proxy_headers=False,
        log_config=log_config,
    )
