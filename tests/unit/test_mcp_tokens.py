"""MCP tokens: the web_mcp_tokens table and scope normalisation."""

from __future__ import annotations

import json

import pytest

from synopticon.db import copy, store
from synopticon.web import auth
from synopticon.web.mcp import registry


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "synopticon.db")
    yield c
    c.close()


def test_migration_creates_table_and_copy_knows_it(conn):
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "web_mcp_tokens" in names
    assert "web_mcp_tokens" in copy.TABLES


def test_create_returns_plaintext_once_and_stores_only_hash(conn):
    token = auth.create_mcp_token(conn, "claude", ["jobs"])
    assert token.startswith("synm_") and len(token) == len("synm_") + 32
    row = conn.execute("SELECT * FROM web_mcp_tokens").fetchone()
    assert token not in tuple(row)
    assert row["token_hash"] == auth._sha256_hex(token)
    assert row["token_prefix"] == token[:13]
    assert json.loads(row["scopes_json"]) == ["read", "jobs"]


def test_validate_returns_token_with_scopes_and_bumps_last_used(conn):
    token = auth.create_mcp_token(conn, "claude", ["config", "review"])
    got = auth.validate_mcp_token(conn, token)
    assert got is not None
    assert got.name == "claude"
    assert got.scopes == frozenset({"read", "config", "review"})
    assert conn.execute("SELECT last_used_at FROM web_mcp_tokens").fetchone()[0] is not None


@pytest.mark.parametrize("bad", ["", "synm_nope", "syn_" + "0" * 32, "x"])
def test_validate_rejects_unknown(conn, bad):
    auth.create_mcp_token(conn, "claude", [])
    assert auth.validate_mcp_token(conn, bad) is None


def test_rest_api_key_is_not_an_mcp_token(conn):
    key = auth.create_api_key(conn, "ci")
    assert auth.validate_mcp_token(conn, key) is None


def test_mcp_token_is_not_an_api_key(conn):
    token = auth.create_mcp_token(conn, "claude", [])
    assert auth.validate_api_key(conn, token) is None


def test_revoke_is_immediate(conn):
    token = auth.create_mcp_token(conn, "claude", [])
    tid = auth.list_mcp_tokens(conn)[0]["id"]
    assert auth.revoke_mcp_token(conn, tid) is True
    assert auth.validate_mcp_token(conn, token) is None
    assert auth.revoke_mcp_token(conn, 9999) is False


def test_list_never_exposes_the_hash(conn):
    token = auth.create_mcp_token(conn, "claude", ["apply"])
    [entry] = auth.list_mcp_tokens(conn)
    assert set(entry) == {"id", "name", "token_prefix", "scopes", "created_at", "last_used_at", "revoked"}
    assert entry["scopes"] == ["read", "apply"]
    assert entry["revoked"] is False
    assert token not in json.dumps(entry)


def test_create_rejects_unknown_scope(conn):
    with pytest.raises(ValueError, match="unknown scope"):
        auth.create_mcp_token(conn, "x", ["root"])
    assert auth.list_mcp_tokens(conn) == []


@pytest.mark.parametrize("scope", ["apply:reassign", "apply:merge"])
def test_dependent_apply_scopes_require_apply(conn, scope):
    with pytest.raises(ValueError, match="'apply'"):
        auth.create_mcp_token(conn, "x", [scope])
    assert auth.create_mcp_token(conn, "x", ["apply", scope])


def test_normalize_scopes_adds_read_and_orders_by_catalogue():
    assert registry.normalize_scopes(["apply", "config"]) == ["read", "config", "apply"]
    assert registry.normalize_scopes([]) == ["read"]
