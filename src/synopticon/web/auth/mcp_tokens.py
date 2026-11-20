"""MCP access tokens: the ``web_mcp_tokens`` table.

A token authenticates the ``/mcp`` endpoint and nothing else. It is never
cached, so revoking it takes effect on the very next request.
"""

from __future__ import annotations

import json
import secrets
import time
from typing import Any

from ...db import Connection
from ..mcp.registry import McpToken, normalize_scopes
from .hashing import _sha256_hex

_TOKEN_BYTES = 16
TOKEN_PREFIX = "synm_"


def create_mcp_token(conn: Connection, name: str, scopes: list[str]) -> str:
    """Mint a token and return the plaintext ``synm_<32hex>`` (shown once).

    Raises ``ValueError`` for an unknown scope or an unmet requirement.
    """
    granted = normalize_scopes(list(scopes or ()))
    token = TOKEN_PREFIX + secrets.token_hex(_TOKEN_BYTES)
    conn.execute(
        "INSERT INTO web_mcp_tokens "
        "(name, token_hash, token_prefix, scopes_json, created_at, revoked) "
        "VALUES (?, ?, ?, ?, ?, 0)",
        (
            name,
            _sha256_hex(token),
            token[: len(TOKEN_PREFIX) + 8],
            json.dumps(granted),
            int(time.time()),
        ),
    )
    conn.commit()
    return token


def _parse_scopes(raw: Any) -> list[str]:
    try:
        return normalize_scopes(json.loads(raw))
    except (ValueError, TypeError):
        return ["read"]


def validate_mcp_token(conn: Connection, token: str) -> McpToken | None:
    """The live token for a presented string, else ``None``; bumps last_used_at."""
    if not token or not token.startswith(TOKEN_PREFIX):
        return None
    row = conn.execute(
        "SELECT id, name, scopes_json, revoked FROM web_mcp_tokens WHERE token_hash = ?",
        (_sha256_hex(token),),
    ).fetchone()
    if row is None or row["revoked"]:
        return None
    conn.execute(
        "UPDATE web_mcp_tokens SET last_used_at = ? WHERE id = ?",
        (int(time.time()), row["id"]),
    )
    conn.commit()
    return McpToken(
        id=int(row["id"]),
        name=row["name"],
        scopes=frozenset(_parse_scopes(row["scopes_json"])),
    )


def revoke_mcp_token(conn: Connection, token_id: int) -> bool:
    """Mark a token revoked. Returns False when no such token exists."""
    cur = conn.execute("UPDATE web_mcp_tokens SET revoked = 1 WHERE id = ?", (token_id,))
    conn.commit()
    return cur.rowcount > 0


def list_mcp_tokens(conn: Connection) -> list[dict[str, Any]]:
    """Tokens for the UI. Never the hash -- only the non-secret prefix."""
    rows = conn.execute(
        "SELECT id, name, token_prefix, scopes_json, created_at, last_used_at, revoked "
        "FROM web_mcp_tokens ORDER BY id"
    ).fetchall()
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "token_prefix": r["token_prefix"],
            "scopes": _parse_scopes(r["scopes_json"]),
            "created_at": r["created_at"],
            "last_used_at": r["last_used_at"],
            "revoked": bool(r["revoked"]),
        }
        for r in rows
    ]
