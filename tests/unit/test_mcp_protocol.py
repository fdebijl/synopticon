"""The /mcp endpoint: gating, authentication, JSON-RPC behaviour."""

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
from synopticon.web.mcp import protocol  # noqa: E402
from synopticon.web.mcp.registry import tool  # noqa: E402


@tool("echo", "Echo the text back.", properties={"text": {"type": "string"}}, scopes=("read",))
def _echo(ctx, args):
    return {"text": args.get("text"), "token": ctx.token.name, "actor": ctx.actor}


@tool("boom", "Always fails.", scopes=("read",))
def _boom(ctx, args):
    raise RuntimeError("secret detail")


@tool("fail", "Reports a problem.", scopes=("read",))
def _fail(ctx, args):
    from synopticon.web.mcp.registry import ToolError

    raise ToolError("nope")


@tool("tune", "Needs config.", scopes=("config",), read_only=False)
def _tune(ctx, args):
    return {"ok": True}


@pytest.fixture(autouse=True)
def tools(monkeypatch):
    monkeypatch.setattr(protocol, "all_tools", lambda: [_echo, _boom, _fail, _tune])


def _settings(tmp_path, enabled=True, **extra):
    return load_settings(
        storage={"data_dir": tmp_path},
        nas={"url": "https://nas.test", "account": "svc", "password": "pw"},
        mcp={"enabled": enabled},
        **extra,
    )


def _app(settings, tmp_path, stub_dist):
    jm = JobManager(tmp_path / "jobs", command_builder=lambda argv: [sys.executable, "-c", "pass"])
    return create_app(settings, job_manager=jm, dist_dir=stub_dist), jm


@pytest.fixture
def env(tmp_path, stub_dist):
    settings = _settings(tmp_path)
    app, jm = _app(settings, tmp_path, stub_dist)
    db = store.connect(settings.storage.db_path)
    auth.create_user(db, "admin", "password123")
    token = auth.create_mcp_token(db, "agent", [])
    yield {"app": app, "db": db, "token": token, "settings": settings}
    db.close()
    jm.shutdown()


@pytest.fixture
def client(env):
    with TestClient(env["app"], follow_redirects=False) as c:
        yield c


def _h(token):
    return {"Authorization": f"Bearer {token}"}


def _rpc(client, token, method, params=None, id=1, **kw):
    body = {"jsonrpc": "2.0", "method": method}
    if id is not None:
        body["id"] = id
    if params is not None:
        body["params"] = params
    return client.post("/mcp", json=body, headers=_h(token), **kw)


# -- gating -------------------------------------------------------------------


def test_disabled_is_404_for_every_method_before_auth(tmp_path, stub_dist):
    settings = _settings(tmp_path, enabled=False)
    app, jm = _app(settings, tmp_path, stub_dist)
    try:
        with TestClient(app, follow_redirects=False) as c:
            for method in ("get", "post", "delete", "put"):
                r = getattr(c, method)("/mcp")
                assert r.status_code == 404
                assert "turned off" in r.json()["error"]
            assert c.post("/mcp/", json={}).status_code == 404
    finally:
        jm.shutdown()


def test_no_token_is_401_with_challenge(client):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


def test_invalid_token_is_401(client):
    assert _rpc(client, "synm_" + "0" * 32, "ping").status_code == 401


def test_revoked_token_is_401(client, env):
    assert _rpc(client, env["token"], "ping").status_code == 200
    tid = auth.list_mcp_tokens(env["db"])[0]["id"]
    auth.revoke_mcp_token(env["db"], tid)
    assert _rpc(client, env["token"], "ping").status_code == 401


def test_rest_api_key_refused_on_mcp(client, env):
    key = auth.create_api_key(env["db"], "ci")
    assert _rpc(client, key, "ping").status_code == 401


def test_session_cookie_refused_on_mcp(client):
    assert client.post("/api/auth/login", json={"username": "admin", "password": "password123"}).status_code == 200
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 401


def test_mcp_token_refused_on_api(client, env):
    assert client.get("/api/stats", headers=_h(env["token"])).status_code == 401


def test_failed_token_is_logged_once_per_window(client, env):
    for _ in range(3):
        _rpc(client, "synm_" + "1" * 32, "ping")
    rows = env["db"].execute("SELECT event, outcome FROM web_auth_log WHERE event = 'mcp_token'").fetchall()
    assert len(rows) == 1 and rows[0]["outcome"] == "failure"


def test_origin_mismatch_is_403(client, env):
    r = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        headers={**_h(env["token"]), "Origin": "https://evil.example"},
    )
    assert r.status_code == 403


def test_matching_origin_is_allowed(client, env):
    r = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        headers={**_h(env["token"]), "Origin": "http://testserver"},
    )
    assert r.status_code == 200


@pytest.mark.parametrize("method", ["get", "delete", "put"])
def test_other_methods_are_405(client, env, method):
    r = getattr(client, method)("/mcp", headers=_h(env["token"]))
    assert r.status_code == 405
    assert r.headers["allow"] == "POST"


def test_bare_and_trailing_slash_both_reach_the_app(client, env):
    body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    assert client.post("/mcp", json=body, headers=_h(env["token"])).status_code == 200
    assert client.post("/mcp/", json=body, headers=_h(env["token"])).status_code == 200


def test_responses_are_no_store(client, env):
    r = _rpc(client, env["token"], "ping")
    assert r.headers["cache-control"] == "no-store"


def test_network_allowlist_still_blocks_mcp(tmp_path, stub_dist):
    settings = _settings(tmp_path, security={"allow_from": ["203.0.113.0/24"]})
    app, jm = _app(settings, tmp_path, stub_dist)
    try:
        db = store.connect(settings.storage.db_path)
        token = auth.create_mcp_token(db, "agent", [])
        db.close()
        with TestClient(app, client=("198.51.100.9", 1234), follow_redirects=False) as c:
            r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers=_h(token))
            assert r.status_code == 403
        with TestClient(app, client=("203.0.113.5", 1234), follow_redirects=False) as c:
            r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers=_h(token))
            assert r.status_code == 200
    finally:
        jm.shutdown()


# -- JSON-RPC -------------------------------------------------------------------


def test_initialize_negotiates_version_and_lists_instructions(client, env):
    r = _rpc(client, env["token"], "initialize", {"protocolVersion": "2025-06-18"})
    result = r.json()["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["serverInfo"]["name"] == "synopticon"
    assert "Apply" in result["instructions"] and "typed" in result["instructions"]
    assert "agent" in result["instructions"]


def test_initialize_unknown_version_gets_newest(client, env):
    result = _rpc(client, env["token"], "initialize", {"protocolVersion": "1999-01-01"}).json()["result"]
    assert result["protocolVersion"] == "2025-11-25"


def test_ping(client, env):
    assert _rpc(client, env["token"], "ping").json() == {"jsonrpc": "2.0", "id": 1, "result": {}}


def test_tools_list_is_filtered_by_scope(client, env):
    names = [t["name"] for t in _rpc(client, env["token"], "tools/list").json()["result"]["tools"]]
    assert names == ["echo", "boom", "fail"]
    token = auth.create_mcp_token(env["db"], "tuner", ["config"])
    names = [t["name"] for t in _rpc(client, token, "tools/list").json()["result"]["tools"]]
    assert "tune" in names


def test_tools_call_success(client, env):
    r = _rpc(client, env["token"], "tools/call", {"name": "echo", "arguments": {"text": "hi"}}).json()
    result = r["result"]
    assert result["structuredContent"] == {"text": "hi", "token": "agent", "actor": "mcp:agent"}
    assert '"text": "hi"' in result["content"][0]["text"]
    assert "isError" not in result


def test_tools_call_tool_error_is_an_error_result_not_a_protocol_error(client, env):
    r = _rpc(client, env["token"], "tools/call", {"name": "fail"}).json()
    assert r["result"] == {"content": [{"type": "text", "text": "nope"}], "isError": True}


def test_tools_call_unexpected_exception_does_not_leak(client, env):
    r = _rpc(client, env["token"], "tools/call", {"name": "boom"}).json()
    assert r["result"]["isError"] is True
    assert r["result"]["content"][0]["text"] == "internal error: RuntimeError"


def test_tools_call_unknown_or_unlisted_tool_is_invalid_params(client, env):
    for name in ("missing", "tune"):
        r = _rpc(client, env["token"], "tools/call", {"name": name}).json()
        assert r["error"]["code"] == -32602


def test_notification_is_202_with_empty_body(client, env):
    r = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=_h(env["token"]),
    )
    assert r.status_code == 202 and r.content == b""


def test_batch_is_invalid_request(client, env):
    r = client.post("/mcp", json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}], headers=_h(env["token"]))
    assert r.json()["error"]["code"] == -32600


def test_unparseable_body_is_parse_error(client, env):
    r = client.post("/mcp", content=b"{nope", headers=_h(env["token"]))
    assert r.json()["error"]["code"] == -32700


def test_unknown_method(client, env):
    assert _rpc(client, env["token"], "resources/list").json()["error"]["code"] == -32601


def test_oversized_body_is_413(client, env):
    r = client.post("/mcp", content=b"x" * (protocol.MAX_BODY_BYTES + 1), headers=_h(env["token"]))
    assert r.status_code == 413
