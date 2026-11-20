"""REST routes that manage MCP tokens: human-only, validated."""

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
from synopticon.web.mcp.registry import SCOPES  # noqa: E402


@pytest.fixture
def env(tmp_path, stub_dist):
    settings = load_settings(
        storage={"data_dir": tmp_path},
        nas={"url": "https://nas.test", "account": "svc", "password": "pw"},
    )
    jm = JobManager(tmp_path / "jobs", command_builder=lambda argv: [sys.executable, "-c", "pass"])
    app = create_app(settings, job_manager=jm, dist_dir=stub_dist)
    db = store.connect(settings.storage.db_path)
    auth.create_user(db, "admin", "password123")
    yield {"app": app, "db": db}
    db.close()
    jm.shutdown()


@pytest.fixture
def client(env):
    with TestClient(env["app"], follow_redirects=False) as c:
        assert c.post("/api/auth/login", json={"username": "admin", "password": "password123"}).status_code == 200
        yield c


def test_status_lists_the_scope_catalogue(client):
    body = client.get("/api/mcp/status").json()
    assert body["enabled"] is False
    assert body["endpoint"] == "/mcp"
    assert [s["id"] for s in body["scopes"]] == [s.id for s in SCOPES]
    merge = next(s for s in body["scopes"] if s["id"] == "apply:merge")
    assert merge["requires"] == ["apply"] and merge["danger"] == "nas"
    assert set(merge) == {"id", "label", "description", "danger", "requires"}


def test_status_reflects_enabled(tmp_path, stub_dist):
    settings = load_settings(storage={"data_dir": tmp_path}, mcp={"enabled": True})
    jm = JobManager(tmp_path / "jobs", command_builder=lambda argv: [sys.executable, "-c", "pass"])
    try:
        app = create_app(settings, job_manager=jm, dist_dir=stub_dist)
        db = store.connect(settings.storage.db_path)
        auth.create_user(db, "admin", "password123")
        db.close()
        with TestClient(app) as c:
            c.post("/api/auth/login", json={"username": "admin", "password": "password123"})
            assert c.get("/api/mcp/status").json()["enabled"] is True
    finally:
        jm.shutdown()


def test_create_list_revoke_roundtrip(client, env):
    r = client.post("/api/mcp/tokens", json={"name": "claude", "scopes": ["config", "jobs"]})
    assert r.status_code == 201
    body = r.json()
    assert body["token"].startswith("synm_")
    assert body["name"] == "claude"
    assert body["scopes"] == ["read", "config", "jobs"]

    [entry] = client.get("/api/mcp/tokens").json()["tokens"]
    assert entry["name"] == "claude" and entry["revoked"] is False
    assert entry["scopes"] == ["read", "config", "jobs"]
    assert body["token"] not in str(entry)

    assert client.post(f"/api/mcp/tokens/{entry['id']}/revoke", json={}).json() == {"ok": True}
    assert client.get("/api/mcp/tokens").json()["tokens"][0]["revoked"] is True
    assert auth.validate_mcp_token(env["db"], body["token"]) is None


def test_create_may_happen_while_disabled(client):
    assert client.post("/api/mcp/tokens", json={"name": "x", "scopes": []}).status_code == 201


@pytest.mark.parametrize(
    "body",
    [
        {"scopes": []},
        {"name": "   ", "scopes": []},
        {"name": 5, "scopes": []},
        {"name": "x", "scopes": ["root"]},
        {"name": "x", "scopes": ["apply:merge"]},
        {"name": "x", "scopes": "apply"},
        {"name": "x", "scopes": [1]},
    ],
)
def test_create_validation_is_422_with_error(client, body):
    r = client.post("/api/mcp/tokens", json=body)
    assert r.status_code == 422
    assert r.json()["error"]


def test_revoke_unknown_is_404(client):
    assert client.post("/api/mcp/tokens/999/revoke", json={}).status_code == 404


def test_routes_are_human_only(env):
    key = auth.create_api_key(env["db"], "ci")
    headers = {"Authorization": f"Bearer {key}"}
    with TestClient(env["app"], follow_redirects=False) as c:
        assert c.get("/api/mcp/status", headers=headers).status_code == 403
        assert c.get("/api/mcp/tokens", headers=headers).status_code == 403
        assert c.post("/api/mcp/tokens", json={"name": "x", "scopes": []}, headers=headers).status_code == 403
        assert c.post("/api/mcp/tokens/1/revoke", json={}, headers=headers).status_code == 403


def test_routes_need_a_session(env):
    with TestClient(env["app"], follow_redirects=False) as c:
        assert c.get("/api/mcp/tokens").status_code == 401
