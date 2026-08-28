"""Accounts: the ``web_users`` table.

Stdlib-only and framework-free: every function operates over a Connection so
the FastAPI layer can wire these in without this module knowing anything about
HTTP. Passwords are scrypt-hashed with a per-user salt; comparisons that matter
use hmac.compare_digest to stay constant-time.
"""

from __future__ import annotations

import secrets
import time
from typing import Any

import hmac

from ...db import Connection, errors as db_errors
from .hashing import _scrypt


class UsernameTakenError(ValueError):
    """Raised when create_user is given a username that already exists."""


def create_user(conn: Connection, username: str, password: str) -> int:
    """Create a user with a scrypt-hashed password; returns the new user id.

    Raises UsernameTakenError if the username is already registered.
    """
    salt = secrets.token_bytes(16)
    derived = _scrypt(password, salt)
    try:
        cur = conn.execute(
            "INSERT INTO web_users (username, password_scrypt, salt, created_at) "
            "VALUES (?, ?, ?, ?)",
            (username, derived, salt, int(time.time())),
        )
    except db_errors.IntegrityError as exc:
        # Recovering from a failed statement means rolling back: PostgreSQL
        # aborts the whole transaction on error, so without this every later
        # statement on this connection fails too. A no-op on SQLite.
        conn.rollback()
        raise UsernameTakenError(username) from exc
    conn.commit()
    return int(cur.lastrowid)


def verify_password(conn: Connection, username: str, password: str) -> int | None:
    """Return the user id if the password is correct, else None (constant-time)."""
    row = conn.execute(
        "SELECT id, password_scrypt, salt FROM web_users WHERE username = ?",
        (username,),
    ).fetchone()
    if row is None:
        # Hash anyway to keep timing roughly uniform whether or not the user exists.
        _scrypt(password, secrets.token_bytes(16))
        return None
    candidate = _scrypt(password, bytes(row["salt"]))
    if hmac.compare_digest(candidate, bytes(row["password_scrypt"])):
        return int(row["id"])
    return None


def has_users(conn: Connection) -> bool:
    """True once at least one admin account exists (drives first-boot claim flow)."""
    return conn.execute("SELECT 1 FROM web_users LIMIT 1").fetchone() is not None


def list_users(conn: Connection) -> list[dict[str, Any]]:
    """List accounts (id/username/created_at only -- never the hash or salt)."""
    rows = conn.execute("SELECT id, username, created_at FROM web_users ORDER BY id").fetchall()
    return [
        {"id": int(r["id"]), "username": r["username"], "created_at": r["created_at"]}
        for r in rows
    ]


def change_password(conn: Connection, user_id: int, new_password: str) -> None:
    """Set a new scrypt-hashed password (fresh salt) for an existing user."""
    salt = secrets.token_bytes(16)
    derived = _scrypt(new_password, salt)
    conn.execute(
        "UPDATE web_users SET password_scrypt = ?, salt = ? WHERE id = ?",
        (derived, salt, user_id),
    )
    conn.commit()


def username_for(conn: Connection, user_id: int) -> str | None:
    """The username for an id, or None. Replaces three inline SELECTs.

    Used by route 11's `provisioning_uri` account label and by routes 11-16's
    throttle key (both W1/section 5.1, R16), and by the sign-in log's
    username on a user-id-only path (W3).
    """
    row = conn.execute("SELECT username FROM web_users WHERE id = ?", (user_id,)).fetchone()
    return None if row is None else str(row["username"])
