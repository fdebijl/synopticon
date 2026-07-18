"""Dashboard page render-smoke tests.

Authed GET / renders the stat-tiles container + pipeline strip on a seeded DB,
and the empty-state CTA on a fresh DB. Hermetic (no NAS, stubbed job manager),
mirroring the fixtures in test_web_api.py.
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
def app(settings, tmp_path):
    jm = JobManager(tmp_path / "jobs", command_builder=_trivial_builder)
    application = create_app(settings, job_manager=jm)
    yield application
    jm.shutdown()


def _seed_user(db, username="admin", password="password123"):
    return auth.create_user(db, username, password)


def _seed_photos(db, space="personal", n=3):
    for i in range(n):
        db.execute(
            "INSERT INTO photos (id, space, synced_at, deleted) VALUES (?,?,?,0)",
            (i + 1, space, store.now()),
        )
    db.commit()


def _login(client):
    return client.post("/login", data={"username": "admin", "password": "password123"})


def test_dashboard_empty_state_on_fresh_db(app, db):
    _seed_user(db)
    with TestClient(app, follow_redirects=False) as c:
        _login(c)
        r = c.get("/")
        assert r.status_code == 200
        # empty-DB CTA is rendered; the tiles/strip containers are not.
        assert 'id="dash-empty"' in r.text
        assert "Run your first sync" in r.text
        assert 'id="stat-tiles"' not in r.text


def test_dashboard_tiles_and_strip_on_seeded_db(app, db):
    _seed_user(db)
    _seed_photos(db, "personal", 3)
    with TestClient(app, follow_redirects=False) as c:
        _login(c)
        r = c.get("/")
        assert r.status_code == 200
        # stat tiles + pipeline strip containers are rendered; no empty CTA.
        assert 'id="stat-tiles"' in r.text
        assert 'id="pipeline-strip"' in r.text
        assert 'id="dash-empty"' not in r.text
        # dashboard.js and its embedded initial stats are wired up.
        assert "dashboard.js" in r.text
        assert "SYN_STATS" in r.text


def test_dashboard_embeds_audit_and_stats_json(app, db):
    _seed_user(db)
    _seed_photos(db, "personal", 1)
    with TestClient(app, follow_redirects=False) as c:
        _login(c)
        r = c.get("/")
        assert r.status_code == 200
        assert "window.SYN_AUDIT" in r.text
        assert "window.SYN_RUNNING" in r.text
