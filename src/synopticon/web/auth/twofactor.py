"""Two-step sign-in: TOTP enrolment, recovery codes, login challenges (SEC1).

Owner: W1. Stdlib-only and framework-free: every function operates over a
Connection so the FastAPI layer can wire these in without this module knowing
anything about HTTP.
"""

from __future__ import annotations

import logging
import secrets
import time
from typing import Any, NamedTuple

from ...db import Connection, errors as db_errors
from .. import clientip, totp
from .hashing import _sha256_hex

log = logging.getLogger("synopticon.web.auth.twofactor")


class TotpAlreadyEnrolledError(ValueError):
    """Raised by start_totp_enrolment when a confirmed factor already exists."""


class NoPendingEnrolmentError(ValueError):
    """There is nothing to confirm: no pending row, or one older than
    ENROLMENT_TTL. Route 12 maps it to 409 with "Setup expired -- start again."
    Returning None for this case as well as for a bad code would collapse a 409
    and a 401 into one value, and the plausible wrong answer is the "check the
    clock on your phone" 401, which names the wrong problem.
    """


#: How long a pending (unconfirmed) enrolment secret is reused rather than
#: replaced, and after which it is treated as absent. See start_totp_enrolment
#: and confirm_totp_enrolment.
ENROLMENT_TTL = 900  # seconds

#: Hard ceiling on live rows in web_login_challenges. See start_login_challenge.
_CHALLENGE_MAX_ROWS = 1000

#: A matched step this far BELOW `last_step` is a clock correction, not a replay:
#: totp.verify only ever tries steps within `skew` of *now*, so a code offered
#: two hours below the recorded step cannot be one that was already used at that
#: step -- it is outside the search window that produced it.
_CLOCK_REWIND_STEPS = 240  # 2 hours at 30 s per step

#: take_login_challenge's own default, and the threshold peek_login_challenge
#: judges "exhausted" by. In practice a row never sits at this count long
#: enough for peek to see it -- take_login_challenge deletes it in the same
#: request that would exceed the cap -- so this only matters defensively.
_DEFAULT_CODE_ATTEMPTS = 5


class PendingEnrolment(NamedTuple):
    """What route 11 needs to render the enrolment card."""

    secret: str  # plaintext base32, unpadded
    pending_expires_in: int  # whole seconds until ENROLMENT_TTL runs out


# -- enrolment --


def totp_status(conn: Connection, user_id: int, *, now: int | None = None) -> dict[str, Any]:
    """{"enrolled", "pending", "confirmed_at", "recovery_remaining",
    "recovery_generated_at", "pending_expires_in"}.

    `pending_expires_in` is None unless an unconfirmed row exists and is still
    inside ENROLMENT_TTL; it is what lets the card say "you have N minutes
    left". `recovery_generated_at` is MAX(web_recovery_codes.created_at) for
    this user, or None -- the fact that decides whether the paper the user
    printed is still the current set.
    """
    now = int(time.time()) if now is None else now
    row = conn.execute(
        "SELECT created_at, confirmed_at FROM web_totp WHERE user_id = ?", (user_id,)
    ).fetchone()
    enrolled = bool(row is not None and row["confirmed_at"] is not None)
    pending = False
    pending_expires_in: int | None = None
    if row is not None and row["confirmed_at"] is None:
        remaining = int(row["created_at"]) + ENROLMENT_TTL - now
        if remaining > 0:
            pending = True
            pending_expires_in = remaining
    gen_row = conn.execute(
        "SELECT MAX(created_at) AS ts FROM web_recovery_codes WHERE user_id = ?", (user_id,)
    ).fetchone()
    return {
        "enrolled": enrolled,
        "pending": pending,
        "confirmed_at": row["confirmed_at"] if row is not None else None,
        "recovery_remaining": count_recovery_codes(conn, user_id),
        "recovery_generated_at": gen_row["ts"] if gen_row is not None else None,
        "pending_expires_in": pending_expires_in,
    }


def totp_enabled(conn: Connection, user_id: int) -> bool:
    """True for a confirmed factor only."""
    row = conn.execute(
        "SELECT 1 FROM web_totp WHERE user_id = ? AND confirmed_at IS NOT NULL", (user_id,)
    ).fetchone()
    return row is not None


def any_totp_enrolled(conn: Connection) -> bool:
    """True when ANY user has a confirmed factor."""
    row = conn.execute("SELECT 1 FROM web_totp WHERE confirmed_at IS NOT NULL LIMIT 1").fetchone()
    return row is not None


def challenge_required(conn: Connection, username: str, user_id: int | None) -> bool:
    """CORRECTION to SEC1. True when `user_id` is a real account with a confirmed
    factor; when `user_id` is None (unknown username, or a wrong password) True
    when any user has one. An enrolled account is therefore indistinguishable
    between a right and a wrong password, and an unknown username looks exactly
    like an enrolled one -- while a NON-enrolled account still signs in in one
    step instead of being handed a challenge it could never complete.

    Consequence the log has to account for: on an enrolled instance EVERY wrong
    password and EVERY unknown username takes the challenge branch, so a
    password spray writes only `pending` rows. See authlog.log_summary.
    """
    del username  # part of the frozen signature; the decision never reads it
    if user_id is not None:
        return totp_enabled(conn, user_id)
    return any_totp_enrolled(conn)


def start_totp_enrolment(
    conn: Connection,
    user_id: int,
    *,
    ttl: int = ENROLMENT_TTL,
    fresh: bool = False,
    now: int | None = None,
) -> PendingEnrolment:
    """Returns the pending secret for this user, minting a fresh one only when
    there is none, when the existing one is older than `ttl`, or when
    `fresh=True`. The returned `pending_expires_in` is what routes 10 and 11
    both report under that exact name -- one quantity, one field name.

    Reuse is the point: the secret exists only in route 11's response body,
    held in a Vue ref. A tab reload, a phone call, or a tab switch and back
    loses it -- and minting a new one silently kills the QR the user has
    already scanned, after which every code they type gets "check the clock on
    your phone", which is the wrong problem on the one flow a user runs exactly
    once. The row is unconfirmed and reachable only by the authenticated user
    who just re-supplied their password (route 11 takes {password}), so
    returning it again costs nothing.

    Raises TotpAlreadyEnrolledError if a confirmed row exists.
    """
    now = int(time.time()) if now is None else now
    row = conn.execute(
        "SELECT secret, created_at, confirmed_at FROM web_totp WHERE user_id = ?", (user_id,)
    ).fetchone()
    if row is not None and row["confirmed_at"] is not None:
        raise TotpAlreadyEnrolledError(str(user_id))

    if row is not None and not fresh and int(row["created_at"]) + ttl > now:
        return PendingEnrolment(
            secret=str(row["secret"]),
            pending_expires_in=int(row["created_at"]) + ttl - now,
        )

    secret = totp.generate_secret()
    conn.execute(
        "INSERT INTO web_totp (user_id, secret, created_at, confirmed_at, last_step) "
        "VALUES (?, ?, ?, NULL, NULL) "
        "ON CONFLICT(user_id) DO UPDATE SET "
        "secret = excluded.secret, created_at = excluded.created_at, "
        "confirmed_at = NULL, last_step = NULL",
        (user_id, secret, now),
    )
    conn.commit()
    return PendingEnrolment(secret=secret, pending_expires_in=ttl)


def confirm_totp_enrolment(
    conn: Connection,
    user_id: int,
    code: str,
    *,
    skew: int = 1,
    recovery_count: int = 10,
    now: int | None = None,
) -> list[str] | None:
    """Verifies `code` against the pending secret. Success: stamps confirmed_at +
    last_step, regenerates recovery codes, returns them in plaintext (the only
    time they exist).

    Raises NoPendingEnrolmentError when there is no pending row, or when the
    pending row is older than ENROLMENT_TTL -- a stale secret is treated as
    absent, never confirmed, because the user's authenticator was seeded from
    a QR the server has since stopped honouring.

    Returns None for a bad code, pending row untouched.

    `recovery_count` is passed by route 12 from
    settings.security.recovery_code_count; the default here exists only for
    direct callers.
    """
    now = int(time.time()) if now is None else now
    row = conn.execute(
        "SELECT secret, created_at, confirmed_at FROM web_totp WHERE user_id = ?", (user_id,)
    ).fetchone()
    if (
        row is None
        or row["confirmed_at"] is not None
        or int(row["created_at"]) + ENROLMENT_TTL <= now
    ):
        raise NoPendingEnrolmentError(str(user_id))

    step = totp.verify(str(row["secret"]), code, now=now, skew=skew)
    if step is None:
        return None

    conn.execute(
        "UPDATE web_totp SET confirmed_at = ?, last_step = ? WHERE user_id = ?",
        (now, step, user_id),
    )
    conn.commit()
    return generate_recovery_codes(conn, user_id, count=recovery_count)


def disable_totp(conn: Connection, user_id: int) -> None:
    """Idempotent; drops recovery codes with the web_totp row."""
    conn.execute("DELETE FROM web_recovery_codes WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM web_totp WHERE user_id = ?", (user_id,))
    conn.commit()


def verify_totp(
    conn: Connection, user_id: int, code: str, *, skew: int = 1, now: int | None = None
) -> bool:
    """Confirmed rows only. The replay guard is the UPDATE's own WHERE clause,
    never a comparison in Python: a read-then-write would let two requests
    carrying the same six digits both accept (SQLite opens a connection per
    request; PostgreSQL is READ COMMITTED).

      step = totp.verify(secret, code, now=now, skew=skew)      # or None
      UPDATE web_totp SET last_step = ?
       WHERE user_id = ? AND confirmed_at IS NOT NULL
         AND (last_step IS NULL OR last_step < ? OR last_step > ?)

    bound with (step, user_id, step, step + _CLOCK_REWIND_STEPS); commit and
    return cur.rowcount == 1. Same shape as consume_recovery_code, for the same
    reason.

    The third disjunct is the clock-rewind escape: a NAS whose fast RTC is
    pulled backwards by NTP would otherwise reject every genuine code until
    wall-clock caught up, with the user staring at "check the clock on your
    phone". Taking it logs one warning. It cannot admit a replay, because
    totp.verify only offers steps within `skew` of now.
    """
    now = int(time.time()) if now is None else now
    row = conn.execute(
        "SELECT secret, last_step FROM web_totp WHERE user_id = ? AND confirmed_at IS NOT NULL",
        (user_id,),
    ).fetchone()
    if row is None:
        return False

    step = totp.verify(str(row["secret"]), code, now=now, skew=skew)
    if step is None:
        return False

    cur = conn.execute(
        "UPDATE web_totp SET last_step = ? "
        "WHERE user_id = ? AND confirmed_at IS NOT NULL "
        "AND (last_step IS NULL OR last_step < ? OR last_step > ?)",
        (step, user_id, step, step + _CLOCK_REWIND_STEPS),
    )
    conn.commit()
    accepted = cur.rowcount == 1
    if accepted and row["last_step"] is not None and step <= int(row["last_step"]):
        log.warning(
            "totp clock-rewind escape accepted for user_id=%s (matched step %s <= last_step %s)",
            user_id,
            step,
            row["last_step"],
        )
    return accepted


# -- recovery codes --


def _format_recovery_code(raw: str) -> str:
    """16 hex chars, hyphenated in groups of four for display."""
    return "-".join(raw[i : i + 4] for i in range(0, len(raw), 4))


def _normalize_recovery_code(raw: str) -> str:
    """Strip the display hyphens/whitespace and lowercase, so a pasted or
    hand-typed code hashes to the same value `generate_recovery_codes` stored."""
    return "".join(ch for ch in (raw or "") if ch.isalnum()).lower()


def generate_recovery_codes(conn: Connection, user_id: int, count: int = 10) -> list[str]:
    """Each code is `secrets.token_hex(8)` -- 64 bits, rendered as 16 hex chars
    and shown to the user hyphenated in groups of four. 64 bits is what
    justifies the unsalted sha256 in migration 0010: at any credible hash rate
    an offline attack on a leaked web_recovery_codes is not worth starting,
    while verification stays one indexed lookup instead of ten scrypt
    derivations. Decimal-digit codes are NOT acceptable here and neither is
    anything under 64 bits -- the fast hash is only sound because the input is
    not guessable.

    Deletes existing codes, inserts sha256 hashes with created_at = now(),
    returns plaintext once. code_hash is UNIQUE across all users, so the insert
    catches db_errors.IntegrityError, calls conn.rollback(), and retries the
    whole set (bounded at 3 attempts before raising).
    """
    now = int(time.time())
    last_exc: BaseException | None = None
    for _ in range(3):
        codes = [secrets.token_hex(8) for _ in range(count)]
        try:
            conn.execute("DELETE FROM web_recovery_codes WHERE user_id = ?", (user_id,))
            conn.executemany(
                "INSERT INTO web_recovery_codes (user_id, code_hash, created_at) "
                "VALUES (?, ?, ?)",
                [(user_id, _sha256_hex(code), now) for code in codes],
            )
        except db_errors.IntegrityError as exc:
            # A code_hash collision across users: vanishingly unlikely at 64
            # bits, but the insert must roll back before this connection can be
            # reused (PostgreSQL aborts the whole transaction on error).
            conn.rollback()
            last_exc = exc
            continue
        conn.commit()
        return [_format_recovery_code(c) for c in codes]
    raise db_errors.IntegrityError(
        "could not generate unique recovery codes after 3 attempts"
    ) from last_exc


def consume_recovery_code(conn: Connection, user_id: int, code: str) -> bool:
    """Single use. Stamps used_at inside the statement's own WHERE
    (... AND used_at IS NULL) and returns rowcount == 1, so two concurrent
    uses cannot both win.
    """
    normalized = _normalize_recovery_code(code)
    if not normalized:
        return False
    cur = conn.execute(
        "UPDATE web_recovery_codes SET used_at = ? "
        "WHERE user_id = ? AND code_hash = ? AND used_at IS NULL",
        (int(time.time()), user_id, _sha256_hex(normalized)),
    )
    conn.commit()
    return cur.rowcount == 1


def count_recovery_codes(conn: Connection, user_id: int) -> int:
    """Unused codes only."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM web_recovery_codes WHERE user_id = ? AND used_at IS NULL",
        (user_id,),
    ).fetchone()
    return int(row["n"]) if row is not None else 0


# -- login challenges (the half-finished sign-in) --


def start_login_challenge(
    conn: Connection, username: str, user_id: int | None, *, ttl_seconds: int = 300
) -> str:
    """The token is `secrets.token_urlsafe(32)` -- 256 bits, the same width as a
    session token, because with a valid code it mints one. Inserts, returns the
    plaintext token; only its sha256 is stored.

    `username` is TRUNCATED to clientip.USERNAME_MAX before the INSERT. It is
    unbounded request input on a table whose only other bound is a row count,
    so without this the ceiling below is a row ceiling and not a byte one.
    (The throttle truncates the same input to the same constant, so the two
    keys still agree across both login steps.)

    BOUNDED TWO WAYS, both on every call, and BOTH STATEMENTS ARE WRITTEN OUT
    HERE because the obvious form of the second is SQLite-only (F5):

      1. DELETE FROM web_login_challenges WHERE expires_at <= ?

      2. n = SELECT COUNT(*) FROM web_login_challenges
         surplus = n - _CHALLENGE_MAX_ROWS + 1        # +1: we are about to add one
         if surplus > 0:
             DELETE FROM web_login_challenges WHERE token_hash IN (
                 SELECT token_hash FROM web_login_challenges
                  ORDER BY expires_at ASC LIMIT ?)
             -- bound with (surplus,)

    `DELETE ... ORDER BY ... LIMIT` is NOT portable: PostgreSQL has no ORDER BY
    or LIMIT on DELETE, and db/dialect.py's _translate_pg rewrites only
    `?`->`%s` and IS/IS NOT, so the SQLite-only form passes through untranslated
    and fails at execution on PostgreSQL alone -- as a 500 on every anonymous
    sign-in attempt once 1000 challenges are live, i.e. under exactly the flood
    the cap exists for. A LIMIT inside a subquery is standard on both. An
    `expires_at <= <cutoff>` form was rejected because expires_at is not unique
    under a fixed TTL and would delete every row sharing the boundary second.

    Purging only expired rows bounds nothing: on an enrolled instance
    challenge_required sends EVERY wrong password and EVERY unknown username
    down this branch, so a flood inserts one row per attempt and they all stay
    live for login_challenge_ttl. Evicting the oldest can drop a real user's
    in-flight sign-in under attack; they get the "start again" 401, which is
    the correct outcome for a table that must not grow without bound.
    """
    now = int(time.time())
    token = secrets.token_urlsafe(32)
    truncated_username = (username or "")[: clientip.USERNAME_MAX]

    conn.execute("DELETE FROM web_login_challenges WHERE expires_at <= ?", (now,))

    n_row = conn.execute("SELECT COUNT(*) AS n FROM web_login_challenges").fetchone()
    n = int(n_row["n"]) if n_row is not None else 0
    surplus = n - _CHALLENGE_MAX_ROWS + 1
    if surplus > 0:
        conn.execute(
            "DELETE FROM web_login_challenges WHERE token_hash IN ("
            "SELECT token_hash FROM web_login_challenges ORDER BY expires_at ASC LIMIT ?)",
            (surplus,),
        )

    conn.execute(
        "INSERT INTO web_login_challenges (token_hash, username, user_id, expires_at, attempts) "
        "VALUES (?, ?, ?, ?, 0)",
        (_sha256_hex(token), truncated_username, user_id, now + ttl_seconds),
    )
    conn.commit()
    return token


def peek_login_challenge(
    conn: Connection, token: str, *, max_attempts: int = _DEFAULT_CODE_ATTEMPTS
) -> tuple[str, int | None] | None:
    """(username, user_id) for a live, non-exhausted challenge, WITHOUT
    consuming an attempt and WITHOUT any write. None when unknown, expired or
    exhausted.

    Step two needs the username before it can compute the pair-tier verdict --
    the throttle keys on (scope, address prefix, username) across both steps --
    and that verdict must be reached before any attempt is spent, or a blocked
    client burns the real user's remaining code attempts by replaying their
    challenge token. take_login_challenge cannot serve here: consuming an
    attempt is its contract.

    This function opens the connection step two's hop A is built around, so it
    is preceded on the loop by an ADDRESS-ONLY verdict -- see throttle.verdict_address
    (D9c). Without that, POSTing random tokens here bought a connection and a
    worker thread per request, unthrottled and unlogged.
    """
    now = int(time.time())
    row = conn.execute(
        "SELECT username, user_id, expires_at, attempts FROM web_login_challenges "
        "WHERE token_hash = ?",
        (_sha256_hex(token),),
    ).fetchone()
    if (
        row is None
        or int(row["expires_at"]) <= now
        or int(row["attempts"]) >= max_attempts
    ):
        return None
    user_id = row["user_id"]
    return str(row["username"]), (int(user_id) if user_id is not None else None)


def take_login_challenge(
    conn: Connection, token: str, *, max_attempts: int = _DEFAULT_CODE_ATTEMPTS
) -> tuple[str, int | None] | None:
    """Consumes one attempt and returns (username, user_id); user_id None means
    the password was wrong at step one.

    ORDER IS EXACT, and it is what makes login_code_attempts = 5 mean five: the
    row is refused when `attempts >= max_attempts` BEFORE any increment, and the
    row is deleted on that refusal. Otherwise `attempts` is incremented and the
    row returned. So submissions 1..5 are judged and the 6th restarts the
    sign-in.

    Returns None when the challenge is unknown, expired, or already at the cap.
    """
    now = int(time.time())
    token_hash = _sha256_hex(token)
    row = conn.execute(
        "SELECT username, user_id, expires_at, attempts FROM web_login_challenges "
        "WHERE token_hash = ?",
        (token_hash,),
    ).fetchone()
    if row is None or int(row["expires_at"]) <= now:
        return None

    if int(row["attempts"]) >= max_attempts:
        conn.execute("DELETE FROM web_login_challenges WHERE token_hash = ?", (token_hash,))
        conn.commit()
        return None

    conn.execute(
        "UPDATE web_login_challenges SET attempts = attempts + 1 WHERE token_hash = ?",
        (token_hash,),
    )
    conn.commit()
    user_id = row["user_id"]
    return str(row["username"]), (int(user_id) if user_id is not None else None)


def delete_login_challenge(conn: Connection, token: str) -> None:
    conn.execute("DELETE FROM web_login_challenges WHERE token_hash = ?", (_sha256_hex(token),))
    conn.commit()


def purge_expired_challenges(conn: Connection) -> int:
    now = int(time.time())
    cur = conn.execute("DELETE FROM web_login_challenges WHERE expires_at <= ?", (now,))
    conn.commit()
    return cur.rowcount


def purge_user_challenges(conn: Connection, user_id: int) -> int:
    """Every live challenge naming this user, by user_id. Called by
    `disable_totp`'s CLI wrapper (`disable-2fa`) so a pending second step
    cannot outlive the factor it was issued for.
    """
    cur = conn.execute("DELETE FROM web_login_challenges WHERE user_id = ?", (user_id,))
    conn.commit()
    return cur.rowcount
