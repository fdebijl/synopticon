"""API keys: the ``web_api_keys`` table. Entirely unchanged by this contract.

API keys are never pinned, never throttled, never challenged for a code, and
never exempt from the network allowlist -- and, from this contract on, never
accepted on the two backup download routes nor on any ``/api/security/*``
route.
"""

from __future__ import annotations

import secrets
import time
from typing import Any

from ...db import Connection
from .hashing import _sha256_hex

_API_KEY_BYTES = 16  # 32 hex chars after hexlify
_API_KEY_PREFIX = "syn_"


def create_api_key(conn: Connection, name: str) -> str:
    """Create a named API key and return the plaintext `syn_<32hex>` (shown once).

    Only the sha256 hash and a short non-secret prefix are stored.
    """
    key = _API_KEY_PREFIX + secrets.token_hex(_API_KEY_BYTES)
    conn.execute(
        "INSERT INTO web_api_keys (name, key_hash, key_prefix, created_at, revoked) "
        "VALUES (?, ?, ?, ?, 0)",
        (name, _sha256_hex(key), key[: len(_API_KEY_PREFIX) + 8], int(time.time())),
    )
    conn.commit()
    return key


def validate_api_key(conn: Connection, key: str) -> int | None:
    """Return the key id for a live (non-revoked) key, else None; bumps last_used_at."""
    if not key:
        return None
    row = conn.execute(
        "SELECT id, revoked FROM web_api_keys WHERE key_hash = ?",
        (_sha256_hex(key),),
    ).fetchone()
    if row is None or row["revoked"]:
        return None
    conn.execute(
        "UPDATE web_api_keys SET last_used_at = ? WHERE id = ?",
        (int(time.time()), row["id"]),
    )
    conn.commit()
    return int(row["id"])


def revoke_api_key(conn: Connection, key_id: int) -> None:
    """Mark a key revoked; subsequent validate_api_key calls return None."""
    conn.execute("UPDATE web_api_keys SET revoked = 1 WHERE id = ?", (key_id,))
    conn.commit()


def list_api_keys(conn: Connection) -> list[dict[str, Any]]:
    """List keys for the UI. Never returns key_hash -- only the non-secret prefix."""
    rows = conn.execute(
        "SELECT id, name, key_prefix, created_at, last_used_at, revoked "
        "FROM web_api_keys ORDER BY id"
    ).fetchall()
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "key_prefix": r["key_prefix"],
            "created_at": r["created_at"],
            "last_used_at": r["last_used_at"],
            "revoked": bool(r["revoked"]),
        }
        for r in rows
    ]
