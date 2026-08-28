"""Sessions and session pinning (SEC4). Owner: W4.

Stdlib-only and framework-free: every function operates over a Connection so
the FastAPI layer can wire these in without this module knowing anything about
HTTP.

``ClientFacts``, ``ip_prefix`` and ``device_key`` live in ``web/clientip.py``
(stdlib only) and are re-exported here, because ``throttle.py`` needs
``ip_prefix`` too and a shared home inside ``auth`` would force ``clientip`` to
import ``auth``.
"""

from __future__ import annotations

import secrets
import time

from ...db import Connection
from ..clientip import ClientFacts, device_key, ip_prefix  # re-exported
from .hashing import _sha256_hex

_SESSION_TOKEN_BYTES = 32  # 256-bit opaque session token
_LAST_SEEN_BUMP_INTERVAL = 60  # seconds; avoid a write on every request

#: The session cookie's name. It lives HERE, not in app.py, because
#: configio.api_change_password (route 6, owned by W2) has to read it to keep
#: the caller's own cookie alive across a revoke -- and app.py imports configio,
#: so `from .app import SESSION_COOKIE` would be a cycle. web/auth/* imports
#: nothing from app.py, so this direction is free. app.py's own
#: `SESSION_COOKIE = "synopticon_session"` becomes
#: `SESSION_COOKIE = auth.SESSION_COOKIE`, so every existing use in app.py is
#: unchanged.
SESSION_COOKIE = "synopticon_session"

PIN_OFF = "off"
PIN_DEVICE = "device"
PIN_DEVICE_NETWORK = "device+network"
PIN_MODES: tuple[str, ...] = (PIN_OFF, PIN_DEVICE, PIN_DEVICE_NETWORK)

_PINNED_MODES = (PIN_DEVICE, PIN_DEVICE_NETWORK)


class SessionPinViolation(Exception):
    """A live session was presented by a client it was not pinned to.

    Raised, not returned: the browser must be told to drop its cookie, which is
    a different reaction from an unknown token. Carries `.destroyed: bool` --
    True when the mismatched row was deleted (a real fingerprint disagreement),
    False when no fingerprint could be computed at all (`client=None` against a
    pinned row) and the row was therefore left in place.
    """

    def __init__(self, message: str, *, destroyed: bool) -> None:
        super().__init__(message)
        self.destroyed = destroyed


def fingerprint(mode: str, client: ClientFacts | None) -> str | None:
    """sha256 over device_key(client.user_agent) for PIN_DEVICE, and that plus
    ip_prefix(client.ip) for PIN_DEVICE_NETWORK. None for PIN_OFF, an unknown
    mode, or client=None. The mode name is inside the hash, so two modes never
    collide.

    device_key, not the raw header: Chrome/Edge bump their UA version roughly
    monthly, so a raw-header pin schedules a forced sign-out (and, on a 2FA
    instance, a full password-and-code dance) for every pinned session on a
    four-week cycle. ip_prefix, not the address: a /24 or /64 is the smallest
    allocation a consumer ISP hands out, and it is normalised so an IPv4-mapped
    IPv6 peer does not read as a different network than the same client's plain
    IPv4 connection.

    Pure CPU on immutable inputs; no lock, nothing shared.
    """
    if client is None or mode not in _PINNED_MODES:
        return None
    parts = [mode, device_key(client.user_agent)]
    if mode == PIN_DEVICE_NETWORK:
        parts.append(ip_prefix(client.ip))
    return _sha256_hex("|".join(parts))


def create_session(
    conn: Connection,
    user_id: int,
    ttl_days: int = 30,
    client: ClientFacts | None = None,
) -> str:
    """Create a session and return the opaque token (only its sha256 hash is stored).

    Reads `web_users.session_pin_mode`; when it is not 'off' and `client` is
    given, stamps `pin_mode` + `pin_hash` on the new row. A session created
    while the setting is 'off', or with no `client` given, is left unpinned.
    """
    token = secrets.token_urlsafe(_SESSION_TOKEN_BYTES)
    now = int(time.time())
    row = conn.execute(
        "SELECT session_pin_mode FROM web_users WHERE id = ?", (user_id,)
    ).fetchone()
    mode = row["session_pin_mode"] if row is not None else PIN_OFF
    pin_mode = mode if (mode != PIN_OFF and client is not None) else None
    pin_hash = fingerprint(pin_mode, client) if pin_mode is not None else None
    conn.execute(
        "INSERT INTO web_sessions "
        "(token_hash, user_id, created_at, expires_at, last_seen_at, pin_mode, pin_hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (_sha256_hex(token), user_id, now, now + ttl_days * 86400, now, pin_mode, pin_hash),
    )
    conn.commit()
    return token


def validate_session(conn: Connection, token: str, client: ClientFacts | None = None) -> int | None:
    """Return the user id for a live session, else None.

    Expired sessions return None (and are removed) BEFORE the pin is ever
    consulted -- an expired row is reaped, never a violation. last_seen_at is
    bumped at most once per minute to avoid a DB write on every single request.

    Raises SessionPinViolation when the row carries a pin_mode and the
    recomputed fingerprint DIFFERS -- and in that case only, deletes the row
    and commits first, because a mismatch is evidence of a replayed cookie
    (`.destroyed` True).

    `client=None` against a pinned row also raises SessionPinViolation --
    failing closed is the only safe direction -- but does NOT delete
    (`.destroyed` False): no fingerprint was computed, so nothing was
    disproved, and a caller that simply forgot to pass the facts would
    otherwise sign the user out of every pinned session with no way back.
    """
    if not token:
        return None
    token_hash = _sha256_hex(token)
    row = conn.execute(
        "SELECT user_id, expires_at, last_seen_at, pin_mode, pin_hash "
        "FROM web_sessions WHERE token_hash = ?",
        (token_hash,),
    ).fetchone()
    if row is None:
        return None
    now = int(time.time())
    if row["expires_at"] <= now:
        conn.execute("DELETE FROM web_sessions WHERE token_hash = ?", (token_hash,))
        conn.commit()
        return None

    pin_mode = row["pin_mode"]
    if pin_mode is not None:
        if client is None:
            raise SessionPinViolation(
                "pinned session presented with no client facts", destroyed=False
            )
        if fingerprint(pin_mode, client) != row["pin_hash"]:
            conn.execute("DELETE FROM web_sessions WHERE token_hash = ?", (token_hash,))
            conn.commit()
            raise SessionPinViolation(
                "session presented by a client it was not pinned to", destroyed=True
            )

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


def delete_user_sessions(conn: Connection, user_id: int, *, except_token: str | None = None) -> int:
    """Revoke every session of one user; returns the number removed.

    `except_token` keeps the caller's own cookie alive (a credential-change
    route revoking every *other* session). Default None preserves
    reset-password's behaviour of revoking everything.
    """
    if except_token is not None:
        cur = conn.execute(
            "DELETE FROM web_sessions WHERE user_id = ? AND token_hash != ?",
            (user_id, _sha256_hex(except_token)),
        )
    else:
        cur = conn.execute("DELETE FROM web_sessions WHERE user_id = ?", (user_id,))
    conn.commit()
    return cur.rowcount


def count_user_sessions(conn: Connection, user_id: int, *, except_token: str | None = None) -> int:
    """Live (unexpired) sessions for this user, optionally excluding the caller's
    own cookie. Route 15's `other_sessions` and nothing else.

    A count, never a list: a per-device inventory (when, from where, pinned or
    not) is exactly what someone holding a stolen cookie would want to read, and
    nothing in this contract needs it. (It is NOT because the rows are secret --
    `web_sessions` deliberately stays in the database snapshot, since a pin_hash
    is a digest of facts a backup holder can already observe.)
    """
    now = int(time.time())
    if except_token is not None:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM web_sessions "
            "WHERE user_id = ? AND expires_at > ? AND token_hash != ?",
            (user_id, now, _sha256_hex(except_token)),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM web_sessions WHERE user_id = ? AND expires_at > ?",
            (user_id, now),
        ).fetchone()
    return int(row["n"])


def purge_expired(conn: Connection) -> int:
    """Delete all expired sessions; returns the number removed."""
    cur = conn.execute("DELETE FROM web_sessions WHERE expires_at <= ?", (int(time.time()),))
    conn.commit()
    return cur.rowcount


def get_pin_mode(conn: Connection, user_id: int) -> str:
    """The account's session-pinning setting. 'off' for an unknown user."""
    row = conn.execute(
        "SELECT session_pin_mode FROM web_users WHERE id = ?", (user_id,)
    ).fetchone()
    return PIN_OFF if row is None else str(row["session_pin_mode"])


def set_pin_mode(
    conn: Connection,
    user_id: int,
    mode: str,
    *,
    keep_token: str | None = None,
    client: ClientFacts | None = None,
) -> int:
    """Change the setting and bring existing sessions into line: `keep_token` is
    re-pinned in place under the new mode, every other session of that user is
    deleted, and that count is returned. This IS the revocation for a pin
    change -- callers must not also call delete_user_sessions, which would
    double-count and report a number the user cannot reconcile.

    `keep_token=None` with `mode=PIN_OFF` is the CLI's shape (`session-pin`):
    every session is unpinned in place rather than deleted -- pin_mode and
    pin_hash are set to NULL on every row for that user -- because a command
    whose whole job is ending a pin loop must not also sign the user out of the
    browser they are about to use. That is the ONLY case where rows are updated
    rather than deleted, and it is only reachable with PIN_OFF.

    Raises ValueError outside PIN_MODES, and ALSO when `mode` is not PIN_OFF
    while `client` is None: stamping pin_mode with a NULL pin_hash produces a
    session whose recomputed fingerprint can only differ, so it destroys itself
    on its owner's very next request. There is no defensible reading of "pin
    this session to nothing".
    """
    if mode not in PIN_MODES:
        raise ValueError(f"unknown session pin mode: {mode!r}")
    if mode != PIN_OFF and client is None:
        raise ValueError("session pinning needs client facts to pin the session to")

    conn.execute("UPDATE web_users SET session_pin_mode = ? WHERE id = ?", (mode, user_id))

    if mode == PIN_OFF and keep_token is None:
        conn.execute(
            "UPDATE web_sessions SET pin_mode = NULL, pin_hash = NULL WHERE user_id = ?",
            (user_id,),
        )
        conn.commit()
        return 0

    new_pin_mode = mode if mode != PIN_OFF else None
    new_pin_hash = fingerprint(mode, client)
    if keep_token is not None:
        keep_hash = _sha256_hex(keep_token)
        conn.execute(
            "UPDATE web_sessions SET pin_mode = ?, pin_hash = ? WHERE token_hash = ?",
            (new_pin_mode, new_pin_hash, keep_hash),
        )
        cur = conn.execute(
            "DELETE FROM web_sessions WHERE user_id = ? AND token_hash != ?",
            (user_id, keep_hash),
        )
    else:
        cur = conn.execute("DELETE FROM web_sessions WHERE user_id = ?", (user_id,))
    conn.commit()
    return cur.rowcount


def cache_key(token: str, client: ClientFacts | None) -> str:
    """The auth middleware's cache key for this cookie AS PRESENTED BY THIS
    CLIENT: "s:<sha256(token)>:<sha256(facts)>". The facts are in the key so a
    cached verdict can never be replayed by a different client. The facts half
    goes through device_key/ip_prefix, so an honest browser keeps a stable key
    across a version bump.
    """
    if client is None:
        facts = ""
    else:
        facts = f"{device_key(client.user_agent)}|{ip_prefix(client.ip)}"
    return f"s:{_sha256_hex(token)}:{_sha256_hex(facts)}"


def cache_prefix(token: str) -> str:
    """"s:<sha256(token)>:" -- every cache_key for this token starts with it, so
    one revocation drops all of them. Used by logout and by the pin-violation
    sweep.
    """
    return f"s:{_sha256_hex(token)}:"
