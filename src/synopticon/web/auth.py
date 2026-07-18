"""Web GUI authentication: users, sessions, API keys, login rate limiting.

Stdlib-only and framework-free by design: every function operates over a
sqlite3.Connection (the same one the rest of the app uses) so the FastAPI layer
that lands later can wire these in without this module knowing anything about
HTTP. Secrets are never stored in plaintext -- passwords are scrypt-hashed with a
per-user salt, session tokens and API keys are stored as their sha256 hash, and
comparisons that matter use hmac.compare_digest to stay constant-time.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import time
from typing import Any, Callable

# scrypt work factors. n must be a power of two; these are the interactive-login
# parameters recommended for scrypt (n=2**14, r=8, p=1) -- a good balance for a
# self-hosted single-admin GUI without pulling in a password-hashing dependency.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SALT_BYTES = 16

# maxmem must be large enough for the chosen (n, r, p); the default 32 MiB is not.
_SCRYPT_MAXMEM = 128 * _SCRYPT_R * _SCRYPT_N * 2

_SESSION_TOKEN_BYTES = 32  # 256-bit opaque session token
_API_KEY_BYTES = 16  # 32 hex chars after hexlify
_API_KEY_PREFIX = "syn_"
_LAST_SEEN_BUMP_INTERVAL = 60  # seconds; avoid a write on every request


# -- password hashing --------------------------------------------------------


def _scrypt(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
        maxmem=_SCRYPT_MAXMEM,
    )


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# -- users -------------------------------------------------------------------


class UsernameTakenError(ValueError):
    """Raised when create_user is given a username that already exists."""


def create_user(conn: sqlite3.Connection, username: str, password: str) -> int:
    """Create a user with a scrypt-hashed password; returns the new user id.

    Raises UsernameTakenError if the username is already registered.
    """
    salt = secrets.token_bytes(_SALT_BYTES)
    derived = _scrypt(password, salt)
    try:
        cur = conn.execute(
            "INSERT INTO web_users (username, password_scrypt, salt, created_at) "
            "VALUES (?, ?, ?, ?)",
            (username, derived, salt, int(time.time())),
        )
    except sqlite3.IntegrityError as exc:
        raise UsernameTakenError(username) from exc
    conn.commit()
    return int(cur.lastrowid)


def verify_password(conn: sqlite3.Connection, username: str, password: str) -> int | None:
    """Return the user id if the password is correct, else None (constant-time)."""
    row = conn.execute(
        "SELECT id, password_scrypt, salt FROM web_users WHERE username = ?",
        (username,),
    ).fetchone()
    if row is None:
        # Hash anyway to keep timing roughly uniform whether or not the user exists.
        _scrypt(password, secrets.token_bytes(_SALT_BYTES))
        return None
    candidate = _scrypt(password, bytes(row["salt"]))
    if hmac.compare_digest(candidate, bytes(row["password_scrypt"])):
        return int(row["id"])
    return None


def has_users(conn: sqlite3.Connection) -> bool:
    """True once at least one admin account exists (drives first-boot claim flow)."""
    return conn.execute("SELECT 1 FROM web_users LIMIT 1").fetchone() is not None


def change_password(conn: sqlite3.Connection, user_id: int, new_password: str) -> None:
    """Set a new scrypt-hashed password (fresh salt) for an existing user."""
    salt = secrets.token_bytes(_SALT_BYTES)
    derived = _scrypt(new_password, salt)
    conn.execute(
        "UPDATE web_users SET password_scrypt = ?, salt = ? WHERE id = ?",
        (derived, salt, user_id),
    )
    conn.commit()


# -- sessions ----------------------------------------------------------------


def create_session(conn: sqlite3.Connection, user_id: int, ttl_days: int = 30) -> str:
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


def validate_session(conn: sqlite3.Connection, token: str) -> int | None:
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


def delete_session(conn: sqlite3.Connection, token: str) -> None:
    """Log out: remove the session for this token (no-op if unknown)."""
    conn.execute("DELETE FROM web_sessions WHERE token_hash = ?", (_sha256_hex(token),))
    conn.commit()


def purge_expired(conn: sqlite3.Connection) -> int:
    """Delete all expired sessions; returns the number removed."""
    cur = conn.execute("DELETE FROM web_sessions WHERE expires_at <= ?", (int(time.time()),))
    conn.commit()
    return cur.rowcount


# -- API keys ----------------------------------------------------------------


def create_api_key(conn: sqlite3.Connection, name: str) -> str:
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


def validate_api_key(conn: sqlite3.Connection, key: str) -> int | None:
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


def revoke_api_key(conn: sqlite3.Connection, key_id: int) -> None:
    """Mark a key revoked; subsequent validate_api_key calls return None."""
    conn.execute("UPDATE web_api_keys SET revoked = 1 WHERE id = ?", (key_id,))
    conn.commit()


def list_api_keys(conn: sqlite3.Connection) -> list[dict[str, Any]]:
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


# -- login rate limiting -----------------------------------------------------


class LoginRateLimiter:
    """In-memory per-(ip, username) exponential backoff on failed logins.

    After a failure, that (ip, username) pair is locked out for `base` seconds,
    doubling with each consecutive failure up to `cap`. A success resets the pair.
    Purely in-memory (backoff should not survive a restart, and it is a soft DoS
    guard, not an audit record). The clock is injectable so tests need no sleeps.
    """

    def __init__(
        self,
        base_seconds: float = 2.0,
        cap_seconds: float = 300.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._base = base_seconds
        self._cap = cap_seconds
        self._clock = clock or time.monotonic
        # (ip, username) -> [consecutive_failures, locked_until_ts]
        self._state: dict[tuple[str, str], list[float]] = {}

    def _key(self, ip: str, username: str) -> tuple[str, str]:
        return (ip, username)

    def check(self, ip: str, username: str) -> bool:
        """True if a login attempt is currently allowed for this (ip, username)."""
        entry = self._state.get(self._key(ip, username))
        if entry is None:
            return True
        return self._clock() >= entry[1]

    def record_failure(self, ip: str, username: str) -> None:
        """Register a failed attempt and (re)arm the backoff window."""
        key = self._key(ip, username)
        entry = self._state.get(key)
        failures = (int(entry[0]) if entry else 0) + 1
        delay = min(self._base * (2 ** (failures - 1)), self._cap)
        self._state[key] = [failures, self._clock() + delay]

    def record_success(self, ip: str, username: str) -> None:
        """Clear all backoff for this (ip, username) after a successful login."""
        self._state.pop(self._key(ip, username), None)
