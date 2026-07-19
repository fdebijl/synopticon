"""Dashboard route serving tests.

The dashboard is now a Vue view (``DashboardView``) that fetches ``/api/stats``
+ ``/api/audit`` client-side — the server no longer renders stat tiles / the
pipeline strip / an empty-state CTA. What the backend still owes here is: serve
the SPA shell to an authenticated ``GET /`` and redirect an unauthenticated one
to login. Hermetic (no NAS, stubbed job manager, injected stub dist).
"""

from __future__ import annotations

import sys

import pytest

from synopticon.config import load_settings
from synopticon.db import store
from synopticon.web import auth

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from synopticon.web.app import create_app  # noqa: E402
from synopticon.web.jobs import JobManager  # noqa: E402


@pytest.fixture
def settings(tmp_path):
    return load_settings(
        storage={"data_dir": tmp_path},
        nas={"url": "https://nas.test", "account": "svc", "password": "pw"},
    )


@pytest.fixture
def db(settings):
    c = store.connect(settings.storage.db_path)
    yield c
    c.close()


def _trivial_builder(argv):
    return [sys.executable, "-c", "import sys; sys.exit(0)"]


@pytest.fixture
def app(settings, tmp_path, stub_dist):
    jm = JobManager(tmp_path / "jobs", command_builder=_trivial_builder)
    application = create_app(settings, job_manager=jm, dist_dir=stub_dist)
    yield application
    jm.shutdown()


def _seed_user(db, username="admin", password="password123"):
    return auth.create_user(db, username, password)


def _login(client):
    return client.post(
        "/api/auth/login", json={"username": "admin", "password": "password123"}
    )


def test_dashboard_serves_spa_shell_when_authenticated(app, db):
    _seed_user(db)
    with TestClient(app, follow_redirects=False) as c:
        _login(c)
        r = c.get("/")
        assert r.status_code == 200
        # the SPA shell is served; the Vue app mounts into #app and fetches data.
        assert '<div id="app">' in r.text


def test_dashboard_redirects_to_login_when_unauthenticated(app, db):
    _seed_user(db)  # users exist, no session on this client
    with TestClient(app, follow_redirects=False) as c:
        r = c.get("/")
        assert r.status_code == 302
        assert r.headers["location"].startswith("/login")
