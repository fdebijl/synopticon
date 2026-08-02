"""Responsiveness invariants for the single-process web GUI.

Two properties, both of which have regressed before and are invisible in a
normal functional test (the app still returns 200 — it just does so seconds
late, and takes every concurrent request down with it):

1. **The web process never imports the extraction image stack.** ``cv2`` (and
   ``onnxruntime``) cost hundreds of milliseconds warm and seconds with a cold
   page cache. Paying that inside a request handler on the only uvicorn process
   stalls every other in-flight request with it. ``/api/stats`` used to reach
   ``pipeline.runner`` for ``pipeline_version``; it now uses the leaf
   ``pipeline.version``.

2. **The watchdog reports each stall mode.** A client-side capture cannot tell a
   blocked event loop from a saturated worker pool from one slow handler — every
   one of them looks like "unrelated requests all finished at the same instant".
   The server has to say which, or the next incident is guesswork again.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import textwrap
import time

import pytest

from synopticon.config import load_settings

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from synopticon.web.app import create_app  # noqa: E402
from synopticon.web.jobs import JobManager  # noqa: E402

#: Modules whose import cost must never land in a request handler.
_FORBIDDEN_IN_WEB_PROCESS = ("cv2", "onnxruntime", "torch")


@pytest.fixture
def settings(tmp_path):
    return load_settings(
        storage={"data_dir": tmp_path},
        nas={"url": "https://nas.test", "account": "svc", "password": "pw"},
    )


@pytest.fixture
def dist(tmp_path):
    d = tmp_path / "dist"
    d.mkdir()
    (d / "index.html").write_text("<html>spa</html>", encoding="utf-8")
    return d


def test_serving_the_api_never_imports_the_image_stack(tmp_path):
    """Import the web modules *and run the stats path*, then check sys.modules.

    Runs in a subprocess on purpose: pytest's session imports cv2 for the
    pipeline tests, which would mask exactly the regression this guards against.
    ``gather_stats`` is actually executed (with a manifest present, so the
    ``models_ready`` branch is taken) — an unused-import check would pass even if
    the lazy import inside ``_pipeline_version_cached`` reached for ``runner``.
    """
    models = tmp_path / "models"
    models.mkdir()
    (models / "manifest.json").write_text(
        '{"scrfd_10g_bnkps": {"file": "scrfd_10g_bnkps.onnx", "sha256": "x"}}',
        encoding="utf-8",
    )
    script = textwrap.dedent(
        f"""
        import sys
        import synopticon.web.app         # noqa: F401
        import synopticon.web.ops_routes  # noqa: F401
        import synopticon.pipeline.crops  # noqa: F401  (only for crops_disk_usage)
        from synopticon.config import load_settings
        from synopticon.db import store
        from synopticon.web.stats import gather_stats

        settings = load_settings(
            storage={{"data_dir": {str(tmp_path)!r}, "models_dir": {str(models)!r}}}
        )
        conn = store.connect(settings.storage.db_path)
        stats = gather_stats(conn, settings)
        assert stats["extract"]["pipeline_version"], stats["extract"]
        print(",".join(m for m in {_FORBIDDEN_IN_WEB_PROCESS!r} if m in sys.modules))
        """
    )
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "", f"web process imported: {out.stdout.strip()}"


def test_pipeline_version_is_re_exported_by_runner():
    """Existing ``from .runner import pipeline_version`` sites keep working."""
    pytest.importorskip("cv2")
    from synopticon.pipeline.runner import pipeline_version as via_runner
    from synopticon.pipeline.version import pipeline_version as via_leaf

    assert via_runner is via_leaf


def _splice_before_catch_all(app, prefix: str) -> None:
    """Move routes under ``prefix`` ahead of the SPA catch-all.

    ``create_app`` registers ``/{path:path}`` last on purpose, so anything added
    afterwards is unreachable. Tests that need a probe route have to reorder.
    """
    routes = app.router.routes
    catch_all = next(r for r in routes if getattr(r, "path", "") == "/{path:path}")
    probes = [r for r in routes if getattr(r, "path", "").startswith(prefix)]
    for r in [*probes, catch_all]:
        routes.remove(r)
    routes.extend([*probes, catch_all])


def test_watchdog_names_a_blocked_event_loop(settings, dist, tmp_path, caplog):
    """Sync work on the loop is reported as loop *lag*, naming the request."""
    jm = JobManager(tmp_path / "jobs", command_builder=lambda argv: [sys.executable, "-c", ""])
    app = create_app(settings, job_manager=jm, dist_dir=dist)

    @app.get("/api/probe/block")
    async def block():  # async def -> runs on the event loop
        time.sleep(1.5)
        return {"ok": True}

    _splice_before_catch_all(app, "/api/probe")
    with caplog.at_level(logging.WARNING, logger="synopticon.web"), TestClient(app) as tc:
        tc.post("/api/auth/create-account", json={"username": "a", "password": "pw"})
        assert tc.get("/api/probe/block").status_code == 200
    jm.shutdown()

    stalls = [r.getMessage() for r in caplog.records if "event loop stalled" in r.getMessage()]
    assert stalls, "a 1.5s sync sleep on the loop went unreported"
    assert "/api/probe/block" in stalls[0]


def test_watchdog_reports_a_slow_handler_without_crying_stall(
    settings, dist, tmp_path, caplog
):
    """A threadpooled slow handler is logged, but the loop is *not* blamed."""
    jm = JobManager(tmp_path / "jobs", command_builder=lambda argv: [sys.executable, "-c", ""])
    app = create_app(settings, job_manager=jm, dist_dir=dist)

    @app.get("/api/probe/slow")
    def slow():  # sync def -> Starlette threadpools it, loop stays free
        time.sleep(3.2)
        return {"ok": True}

    _splice_before_catch_all(app, "/api/probe")
    with caplog.at_level(logging.WARNING, logger="synopticon.web"), TestClient(app) as tc:
        tc.post("/api/auth/create-account", json={"username": "a", "password": "pw"})
        assert tc.get("/api/probe/slow").status_code == 200
    jm.shutdown()

    messages = [r.getMessage() for r in caplog.records]
    assert any("slow request: GET /api/probe/slow" in m for m in messages)
    assert not any("event loop stalled" in m for m in messages)


def test_watchdog_reports_external_cpu_pressure(settings, dist, tmp_path, caplog):
    """Every stall line carries the machine's own contention numbers.

    The three internal stall modes cannot describe a fourth: the process was
    runnable and the kernel scheduled something else (a job subprocess with
    every core). That reads as perfect health from inside — no loop lag, an idle
    threadpool — so the pressure snapshot is the only way to tell it apart.
    """
    jm = JobManager(tmp_path / "jobs", command_builder=lambda argv: [sys.executable, "-c", ""])
    app = create_app(settings, job_manager=jm, dist_dir=dist)

    @app.get("/api/probe/slow")
    def slow():
        time.sleep(3.2)
        return {"ok": True}

    _splice_before_catch_all(app, "/api/probe")
    with caplog.at_level(logging.WARNING, logger="synopticon.web"), TestClient(app) as tc:
        tc.post("/api/auth/create-account", json={"username": "a", "password": "pw"})
        assert tc.get("/api/probe/slow").status_code == 200
    jm.shutdown()

    line = next(m for m in (r.getMessage() for r in caplog.records) if "slow request" in m)
    # PSI is Linux-only; the load average is the portable half.
    assert "load " in line or "pressure unavailable" in line


def test_anonymous_requests_never_touch_the_database(settings, dist, tmp_path):
    """No credential + an account already exists => no connection is opened.

    A cookieless loopback healthcheck polling ``/api/auth/me`` used to open a
    SQLite connection per hit purely to be told it was anonymous, and so did
    every 401. Both paths now answer from the ``have_users`` latch.
    """
    jm = JobManager(tmp_path / "jobs", command_builder=lambda argv: [sys.executable, "-c", ""])
    app = create_app(settings, job_manager=jm, dist_dir=dist)

    with TestClient(app) as tc:
        tc.post("/api/auth/create-account", json={"username": "a", "password": "pw"})
        tc.cookies.clear()
        # The `have_users` latch is only consulted (and set) by a lookup, and
        # create-account does not flip it — one more request settles it.
        tc.get("/api/auth/me")

        from synopticon.db import store

        opened = 0
        real_connect = store.connect

        def counting_connect(*a, **kw):
            nonlocal opened
            opened += 1
            return real_connect(*a, **kw)

        store.connect = counting_connect
        try:
            assert tc.get("/api/auth/me").json()["authenticated"] is False
            assert tc.get("/api/stats").status_code == 401
        finally:
            store.connect = real_connect
    jm.shutdown()

    assert opened == 0, f"anonymous requests opened {opened} DB connection(s)"


def test_sse_disconnect_does_not_raise_no_response_returned(
    settings, dist, tmp_path, caplog
):
    """Abandoning a job stream must not log a spurious middleware traceback.

    ``BaseHTTPMiddleware`` turns "inner app finished without sending a response"
    — which is what a mid-stream client disconnect looks like — into
    ``RuntimeError: No response returned.``. The middleware stack is pure ASGI
    precisely so that cannot happen; this locks it in.
    """
    jm = JobManager(tmp_path / "jobs", command_builder=lambda argv: [sys.executable, "-c", ""])
    app = create_app(settings, job_manager=jm, dist_dir=dist)

    with caplog.at_level(logging.ERROR), TestClient(app) as tc:
        tc.post("/api/auth/create-account", json={"username": "a", "password": "pw"})
        job_id = tc.post("/api/jobs", json={"name": "cluster", "params": {}}).json()["job_id"]
        # Open the stream and walk away after the first chunk.
        with tc.stream("GET", f"/api/jobs/{job_id}/stream?after=0") as resp:
            assert resp.status_code == 200
            for _ in resp.iter_raw():
                break
    jm.shutdown()

    assert not any("No response returned" in r.getMessage() for r in caplog.records)


def test_middleware_stack_is_pure_asgi(settings, dist, tmp_path):
    """No ``BaseHTTPMiddleware`` in the stack.

    Each such layer relays the response through a memory object stream inside a
    task group — nine hops per SSE event with three of them — and is the source
    of the spurious ``No response returned.`` on client disconnect.
    """
    from starlette.middleware.base import BaseHTTPMiddleware

    jm = JobManager(tmp_path / "jobs", command_builder=lambda argv: [sys.executable, "-c", ""])
    app = create_app(settings, job_manager=jm, dist_dir=dist)
    jm.shutdown()

    offenders = [m.cls.__name__ for m in app.user_middleware if issubclass(m.cls, BaseHTTPMiddleware)]
    assert not offenders, f"BaseHTTPMiddleware layers present: {offenders}"


def test_health_probe_is_io_free_and_unauthenticated(settings, dist, tmp_path):
    """``/api/health`` answers 200 with no credential and opens no connection.

    This is the container liveness probe. During a long ``extract`` the job
    writes crops and commits per photo to the same volume the server reads, so
    every DB-touching handler can queue behind that I/O; a probe pointed at one
    of them (``/api/auth/me``, ``/api/stats``) times out and the orchestrator
    restarts the container out from under the job. The probe must therefore be
    answered before the auth lookup and must touch nothing.
    """
    jm = JobManager(tmp_path / "jobs", command_builder=lambda argv: [sys.executable, "-c", ""])
    app = create_app(settings, job_manager=jm, dist_dir=dist)

    with TestClient(app) as tc:
        # Reachable during first boot, before any account exists.
        assert tc.get("/api/health").json()["ok"] is True

        tc.post("/api/auth/create-account", json={"username": "a", "password": "pw"})
        tc.cookies.clear()

        from synopticon.db import store

        opened = 0
        real_connect = store.connect

        def counting_connect(*a, **kw):
            nonlocal opened
            opened += 1
            return real_connect(*a, **kw)

        store.connect = counting_connect
        try:
            r = tc.get("/api/health")
        finally:
            store.connect = real_connect

        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert opened == 0
    jm.shutdown()


def test_health_handler_is_async_so_it_needs_no_worker_thread(settings, dist, tmp_path):
    """A sync ``def`` handler is dispatched to the AnyIO pool.

    That pool is precisely what a running job starves, so a sync health handler
    would queue behind 40 blocked requests and fail the very probe it exists to
    answer. Locking the coroutine-ness in: the endpoint's correctness is that it
    never leaves the event loop.
    """
    import inspect

    jm = JobManager(tmp_path / "jobs", command_builder=lambda argv: [sys.executable, "-c", ""])
    app = create_app(settings, job_manager=jm, dist_dir=dist)
    route = next(r for r in app.routes if getattr(r, "path", None) == "/api/health")
    assert inspect.iscoroutinefunction(route.endpoint)
    jm.shutdown()
