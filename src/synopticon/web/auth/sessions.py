"""Sessions: the ``web_sessions`` table.

A session token is a 256-bit opaque string handed to the browser in a cookie;
only its sha256 hash is stored, so the table is useless to anyone who reads it.
``last_seen_at`` is bumped at most once a minute -- a review grid issues one
request per crop, and a write on every one of them would be pure noise.
"""

from __future__ import annotations

import secrets
import time

from ...db import Connection
from .hashing import _sha256_hex

SESSION_COOKIE = "synopticon_session"

_SESSION_TOKEN_BYTES = 32  # 256-bit opaque session token
_LAST_SEEN_BUMP_INTERVAL = 60  # seconds; avoid a write on every request


def create_session(conn: Connection, user_id: int, ttl_days: int = 30) -> str:
    """Create a session and return the opaque token (only its sha256 hash is stored)."""
    token = secrets.token_urlsafe(_SESSION_TOKEN_BYTES)
    now = int(time.time())
    conn.execute(
        "INSERT INTO web_sessions (token_hash, user_id, created_at, expires_at, last_seen_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (_sha256_hex(token), user_id, now, now + ttl_days * 86400, now),
    )
    conn.commit()
    return token


def validate_session(conn: Connection, token: str) -> int | None:
    """Return the user id for a live session, else None.

    Expired sessions return None (and are removed). last_seen_at is bumped at most
    once per minute to avoid a DB write on every single request.
    """
    if not token:
        return None
    token_hash = _sha256_hex(token)
    row = conn.execute(
        "SELECT user_id, expires_at, last_seen_at FROM web_sessions WHERE token_hash = ?",
        (token_hash,),
    ).fetchone()
    if row is None:
        return None
    now = int(time.time())
    if row["expires_at"] <= now:
        conn.execute("DELETE FROM web_sessions WHERE token_hash = ?", (token_hash,))
        conn.commit()
        return None
    if row["last_seen_at"] is None or now - row["last_seen_at"] >= _LAST_SEEN_BUMP_INTERVAL:
        conn.execute(
            "UPDATE web_sessions SET last_seen_at = ? WHERE token_hash = ?",
            (now, token_hash),
        )
        conn.commit()
    return int(row["user_id"])


def delete_session(conn: Connection, token: str) -> None:
    """Log out: remove the session for this token (no-op if unknown)."""
    conn.execute("DELETE FROM web_sessions WHERE token_hash = ?", (_sha256_hex(token),))
    conn.commit()


def delete_user_sessions(conn: Connection, user_id: int) -> int:
    """Revoke every session of one user; returns the number removed.

    Used after an out-of-band password reset so a leaked cookie can't outlive the
    credential it was issued against.
    """
    cur = conn.execute("DELETE FROM web_sessions WHERE user_id = ?", (user_id,))
    conn.commit()
    return cur.rowcount


def purge_expired(conn: Connection) -> int:
    """Delete all expired sessions; returns the number removed."""
    cur = conn.execute("DELETE FROM web_sessions WHERE expires_at <= ?", (int(time.time()),))
    conn.commit()
    return cur.rowcount
