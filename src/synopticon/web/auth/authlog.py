"""The sign-in log (SEC3), absorbing SEC5's attempt log: the ``web_auth_log``
table plus ``EventThrottle``, the per-key rate limiter that keeps a flood from
burying real events under its own bookkeeping.

Stdlib-only and framework-free, like the rest of ``web/auth``: every function
takes a Connection and never a Request. Never records a credential -- see
``record_attempt``.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from ...db import Connection, errors as db_errors
from .. import clientip

log = logging.getLogger("synopticon.web.auth.authlog")

_LOG_USERNAME_MAX = clientip.USERNAME_MAX  # one value, one definition
_LOG_IP_MAX = clientip.IP_MAX
_LOG_UA_MAX = clientip.UA_MAX  # app.py uses clientip.UA_MAX directly and never
# reaches into this module's private name
_PRUNE_EVERY = 1000

#: Guards the module-level policy below, the `blocked_log` binding, and the
#: insert counter (section 4.0, D8). Held only for dict/int arithmetic: NEVER
#: across the INSERT, never across prune_auth_log, never across a connection
#: open. record_attempt runs on the event loop and in AnyIO worker threads
#: depending on which caller reached it, and configure() runs at start-up while
#: the scheduler thread may already be reading the policy.
_state_lock = threading.Lock()

AUTH_EVENTS = (
    "login",
    "login_code",
    "logout",
    "create_account",
    "password_change",
    "security_change",
    "api_key",
)
AUTH_OUTCOMES = ("success", "failure", "blocked", "pending")

# Defaults before configure() is ever called, so a test that never calls it
# behaves as documented.
_enabled = True
_max_age_days = 90
_max_rows = 5000
_inserts = 0


class EventThrottle:
    """Per-key minimum interval between recorded events, in memory per process.

    Two callers, one reason. A cron job holding a revoked key polls every few
    seconds; an attacker who ignores a 429 keeps sending. Without this, either
    one's rows alone would blow the retention cap in a day and bury the password
    attempts the log exists to show.

    Keys are ALWAYS an address prefix (clientip.ip_prefix), never a full
    address: keyed on the address, an attacker rotating inside one /64 is
    blocked by the prefix-keyed limiter and still mints one key and one row per
    address per minute -- unbounded rows and unbounded process memory inside
    the mitigation that exists to bound them. Bounded at `max_tracked` with the
    same sweep-then-evict-by-deadline rule as LoginRateLimiter. Clock
    injectable.

    Carries `self._lock = threading.Lock()`, taken by `allow()` for its whole
    body (section 4.0): it is called from the event loop by the login routes
    and from an AnyIO worker by the Bearer-failure branch.

    NOTE the bound this class provides is only as good as the address it is
    handed. Behind a listed proxy that does not overwrite X-Forwarded-For, a
    forged header mints a fresh prefix per request and "one row per minute"
    becomes one row per request -- which is one of the reasons D7's warnings
    exist and one of the things they name.
    """

    def __init__(
        self,
        interval_seconds: float = 60.0,
        max_tracked: int = 4096,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._interval = interval_seconds
        self._max_tracked = max_tracked
        self._clock = clock or time.monotonic
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        """`key` is expected to be "<label>:<ip_prefix>"; callers build it as
        f"blocked:{clientip.ip_prefix(ip)}" / f"apikey:{clientip.ip_prefix(ip)}".
        """
        now = self._clock()
        with self._lock:
            self._sweep(now)
            last = self._last.get(key)
            if last is not None and now - last < self._interval:
                return False
            self._last[key] = now
            while len(self._last) > self._max_tracked:
                oldest_key = min(self._last, key=self._last.get)
                del self._last[oldest_key]
            return True

    def _sweep(self, now: float) -> None:
        expired = [k for k, seen in self._last.items() if now - seen >= self._interval]
        for k in expired:
            del self._last[k]


#: One instance per process, shared by the rejected-API-key path and the
#: rate-limited-login path. 60 s. REPLACED by every configure() call, under
#: _state_lock.
blocked_log = EventThrottle()


def configure(*, enabled: bool, max_age_days: int, max_rows: int) -> None:
    """Bind this process's logging policy. Called ONCE from create_app.

    `authlog` is framework-free and takes no Settings anywhere, so the three
    `[security]` fields that govern it have to arrive by this one call.
    Without it `sign_in_log = False` would be a config field with no consumer,
    and `_PRUNE_EVERY`'s "configured bounds" would have to be hardcoded 90/5000
    -- which silently deletes 90-day-old rows on an instance whose operator set
    `sign_in_log_days = 0` to keep them. Defaults before the call are
    enabled=True, 90, 5000, so a test that never calls it behaves as
    documented.

    It ALSO replaces the module-level `blocked_log` with a fresh EventThrottle.
    Both the policy and that throttle are module globals, and the test suite
    builds several apps in one process (test_web_auth.py and test_login_flow.py
    both do); without the reset, "at most one row per minute" assertions are
    order-dependent. create_app already calls configure exactly once, so this
    costs nothing and needs no separate test hook.

    All four rebinds happen inside `_state_lock` in one critical section, so
    the scheduler thread can never observe a half-swapped policy.
    """
    global _enabled, _max_age_days, _max_rows, blocked_log
    with _state_lock:
        _enabled = enabled
        _max_age_days = max_age_days
        _max_rows = max_rows
        blocked_log = EventThrottle()


def retention_policy() -> dict[str, Any]:
    """{"enabled", "days", "max_rows"} -- the values `configure` was given.

    THE accessor. Route 18's `retention` block reads it, so a card that says
    "kept for 90 days" cannot disagree with what the pruner does, and the
    scheduler's hourly pass reads it to decide whether to run at all.

    Takes `_state_lock` and returns a fresh dict, never a reference to module
    state.
    """
    with _state_lock:
        return {"enabled": _enabled, "days": _max_age_days, "max_rows": _max_rows}


def record_attempt(
    conn: Connection,
    *,
    event: str,
    outcome: str,
    reason: str | None = None,
    username: str | None = None,
    user_id: int | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    """Append one row. NEVER RAISES.

    `conn` is positional and required -- every call site passes the connection
    its own hop already opened.

    Returns immediately, writing nothing, when `configure(enabled=False)` was
    called -- that is what makes the `sign_in_log` switch real.

    An audit table must not be able to lock the admin out, so a database error
    is rolled back (PostgreSQL aborts the whole transaction where SQLite
    shrugs) and logged, not propagated. Callers run it AFTER the write that
    matters has committed, so the rollback can never undo a session. Truncates
    every attacker-supplied string before storing it: username to
    _LOG_USERNAME_MAX, ip to _LOG_IP_MAX, user_agent to _LOG_UA_MAX.

    Every `_PRUNE_EVERY` successful inserts in this process it also calls
    prune_auth_log with the bounds `configure` was given. The scheduler's
    hourly pass is the normal enforcement; this is the backstop that keeps a
    sustained flood from growing the table by hundreds of thousands of rows
    between two passes.

    LOCKING (section 4.0): `_state_lock` is taken twice, for a dict copy and
    for an integer bump-and-test, and is NOT HELD across the INSERT or across
    prune_auth_log. This function runs on the event loop for some callers and
    in an AnyIO worker for others, so the counter and the policy are genuinely
    shared.
    """
    with _state_lock:
        enabled = _enabled
        policy_max_age_days = _max_age_days
        policy_max_rows = _max_rows
    if not enabled:
        return

    if username is not None:
        username = username[:_LOG_USERNAME_MAX]
    if ip is not None:
        ip = ip[:_LOG_IP_MAX]
    if user_agent is not None:
        user_agent = user_agent[:_LOG_UA_MAX]

    try:
        conn.execute(
            "INSERT INTO web_auth_log "
            "(ts, event, outcome, reason, username, user_id, ip, user_agent) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (int(time.time()), event, outcome, reason, username, user_id, ip, user_agent),
        )
        conn.commit()
    except db_errors.DatabaseError:
        conn.rollback()
        log.exception("failed to record sign-in log entry (event=%s outcome=%s)", event, outcome)
        return
    except Exception:  # noqa: BLE001 - an audit table must never break a sign-in
        conn.rollback()
        log.exception("failed to record sign-in log entry (event=%s outcome=%s)", event, outcome)
        return

    global _inserts
    with _state_lock:
        _inserts += 1
        backstop_due = _inserts % _PRUNE_EVERY == 0

    if backstop_due:
        try:
            prune_auth_log(conn, max_age_days=policy_max_age_days, max_rows=policy_max_rows)
        except Exception:  # noqa: BLE001 - the backstop must never break a sign-in either
            conn.rollback()
            log.exception("sign-in log backstop prune failed")


def auth_log(
    conn: Connection,
    *,
    limit: int = 50,
    offset: int = 0,
    outcome: str | None = None,
    event: str | None = None,
    username: str | None = None,
    ip: str | None = None,
    since: int | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """One page, newest first, plus the total matching rows. Filters ANDed;
    `username` and `ip` are exact matches, never a LIKE.

    `ip` is served by idx_web_auth_log_ip. "What else came from this address"
    is the first question anyone asks of a suspicious row, and without the
    filter the answer is a full scan the operator does by eye.

    `outcome='failure'` matches the same predicate log_summary counts (see
    below), i.e. the union of `failure` and `pending`/`password_bad`. Every
    other outcome is an exact match over AUTH_OUTCOMES.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if outcome is not None:
        if outcome == "failure":
            clauses.append(
                "(outcome = 'failure' OR (outcome = 'pending' AND reason = 'password_bad'))"
            )
        else:
            clauses.append("outcome = ?")
            params.append(outcome)
    if event is not None:
        clauses.append("event = ?")
        params.append(event)
    if username is not None:
        clauses.append("username = ?")
        params.append(username)
    if ip is not None:
        clauses.append("ip = ?")
        params.append(ip)
    if since is not None:
        clauses.append("ts >= ?")
        params.append(since)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    total_row = conn.execute(
        f"SELECT COUNT(*) AS n FROM web_auth_log {where}", params
    ).fetchone()
    total = int(total_row["n"]) if total_row is not None else 0

    rows = conn.execute(
        "SELECT id, ts, event, outcome, reason, username, user_id, ip, user_agent "
        f"FROM web_auth_log {where} ORDER BY id DESC LIMIT ? OFFSET ?",
        [*params, int(limit), int(offset)],
    ).fetchall()
    items = [dict(r) for r in rows]
    return items, total


def log_summary(conn: Connection, since_ts: int) -> dict[str, int]:
    """{"total","failed","distinct_ips","distinct_usernames"} in one grouped
    query. No strftime -- the window boundary is computed in Python.

    `failed` counts `outcome = 'failure' OR (outcome = 'pending' AND reason =
    'password_bad')`, NOT `outcome = 'failure'` alone. On an instance where
    anyone has enrolled, challenge_required sends every wrong password and
    every unknown username down the challenge branch, so a password spray
    writes only `pending` rows -- and a naive `failed` count reads zero during
    the attack, which is the one moment the number exists to be read.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN outcome = 'failure' "
        "OR (outcome = 'pending' AND reason = 'password_bad') "
        "THEN 1 ELSE 0 END) AS failed, "
        "COUNT(DISTINCT ip) AS distinct_ips, "
        "COUNT(DISTINCT username) AS distinct_usernames "
        "FROM web_auth_log WHERE ts >= ?",
        (since_ts,),
    ).fetchone()
    return {
        "total": int(row["total"] or 0),
        "failed": int(row["failed"] or 0),
        "distinct_ips": int(row["distinct_ips"] or 0),
        "distinct_usernames": int(row["distinct_usernames"] or 0),
    }


def prune_auth_log(conn: Connection, *, max_age_days: int = 90, max_rows: int = 5000) -> int:
    """Age first, then the row cap. Either bound may be 0 to disable it.

    The row cap is a dialect-safe subselect, no window functions:
      DELETE FROM web_auth_log WHERE id <= (
          SELECT id FROM web_auth_log ORDER BY id DESC LIMIT 1 OFFSET ?)
    which no-ops when fewer rows exist (the subselect yields NULL and
    `id <= NULL` matches nothing on both backends). `id` is unique, which is
    why this shape works here and why start_login_challenge's cap (over the
    non-unique expires_at) needs the different form spelled out in twofactor.py.

    THE CALLER decides whether to run at all: the scheduler skips this pass
    entirely when retention_policy()["enabled"] is False, because `sign_in_log`'s
    own description promises that turning the log off "does not delete the ones
    already there" and an unconditional hourly pass ages them out anyway (R18).
    """
    removed = 0
    if max_age_days > 0:
        cutoff = int(time.time()) - max_age_days * 86400
        cur = conn.execute("DELETE FROM web_auth_log WHERE ts < ?", (cutoff,))
        conn.commit()
        removed += cur.rowcount or 0
    if max_rows > 0:
        cur = conn.execute(
            "DELETE FROM web_auth_log WHERE id <= ("
            "SELECT id FROM web_auth_log ORDER BY id DESC LIMIT 1 OFFSET ?)",
            (max_rows,),
        )
        conn.commit()
        removed += cur.rowcount or 0
    return removed
