"""web.auth.authlog: the sign-in log and its EventThrottle.

Named tests here pin R18 (retention gates the pruner, not the writer) and R19
(configure() must not leak EventThrottle state between tests), the D8 locking
discipline, and section 4.7's one hard prohibition: this module must never be
able to record a credential.
"""

from __future__ import annotations

import inspect
import threading
import time

import pytest

from synopticon.db import store
from synopticon.web import clientip
from synopticon.web.auth import authlog, sessions, twofactor


@pytest.fixture(autouse=True)
def _reset_authlog_state():
    """authlog's policy, blocked_log and insert counter are module globals by
    design (section 4.0) -- reset them around every test so one test's calls
    never change another's outcome."""
    authlog.configure(enabled=True, max_age_days=90, max_rows=5000)
    authlog._inserts = 0
    yield
    authlog.configure(enabled=True, max_age_days=90, max_rows=5000)
    authlog._inserts = 0


def _insert(conn, **kw):
    defaults = dict(event="login", outcome="success")
    defaults.update(kw)
    authlog.record_attempt(conn, **defaults)


def _count(conn) -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM web_auth_log").fetchone()["n"]


def _row_count_at(conn, ts: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM web_auth_log WHERE ts = ?", (ts,)
    ).fetchone()["n"]


# -- record_attempt: basic behaviour and the enabled switch ------------------


def test_record_attempt_writes_a_row(web_conn):
    _insert(web_conn, username="alice", user_id=1, ip="203.0.113.5", user_agent="pytest")
    assert _count(web_conn) == 1
    row = web_conn.execute("SELECT * FROM web_auth_log").fetchone()
    assert row["event"] == "login"
    assert row["outcome"] == "success"
    assert row["username"] == "alice"
    assert row["user_id"] == 1
    assert row["ip"] == "203.0.113.5"
    assert row["user_agent"] == "pytest"


def test_record_attempt_truncates_oversized_fields(web_conn):
    _insert(
        web_conn,
        username="u" * 1000,
        ip="i" * 1000,
        user_agent="a" * 1000,
    )
    row = web_conn.execute("SELECT * FROM web_auth_log").fetchone()
    assert len(row["username"]) == clientip.USERNAME_MAX
    assert len(row["ip"]) == clientip.IP_MAX
    assert len(row["user_agent"]) == clientip.UA_MAX


def test_configure_disabled_writes_nothing(web_conn):
    authlog.configure(enabled=False, max_age_days=90, max_rows=5000)
    _insert(web_conn, username="alice")
    assert _count(web_conn) == 0


def test_record_attempt_never_raises_on_a_database_error(web_conn):
    """An audit table must not be able to lock the admin out: a failing INSERT
    is rolled back and logged, never propagated, and the connection is still
    usable afterwards."""
    web_conn.execute("DROP TABLE web_auth_log")
    web_conn.commit()

    authlog.record_attempt(web_conn, event="login", outcome="failure")  # must not raise

    # the connection itself must still work for the next statement
    assert web_conn.execute("SELECT 1 AS one").fetchone()["one"] == 1


# -- the _PRUNE_EVERY backstop ------------------------------------------------


def test_prune_backstop_uses_configured_bounds(web_conn, monkeypatch):
    """The backstop inside record_attempt must read configure()'s bounds, not
    a hardcoded default -- max_age_days=0 must keep a 100-day-old row."""
    monkeypatch.setattr(authlog, "_PRUNE_EVERY", 5)
    authlog.configure(enabled=True, max_age_days=0, max_rows=0)
    authlog._inserts = 0

    old_ts = int(time.time()) - 100 * 86400
    web_conn.execute(
        "INSERT INTO web_auth_log (ts, event, outcome) VALUES (?, 'login', 'success')",
        (old_ts,),
    )
    web_conn.commit()

    for _ in range(5):  # trips the backstop on the fifth insert
        _insert(web_conn)

    assert _row_count_at(web_conn, old_ts) == 1


def test_prune_backstop_does_not_fire_before_the_threshold(web_conn, monkeypatch):
    monkeypatch.setattr(authlog, "_PRUNE_EVERY", 5)
    authlog.configure(enabled=True, max_age_days=1, max_rows=0)
    authlog._inserts = 0

    old_ts = int(time.time()) - 100 * 86400
    web_conn.execute(
        "INSERT INTO web_auth_log (ts, event, outcome) VALUES (?, 'login', 'success')",
        (old_ts,),
    )
    web_conn.commit()

    for _ in range(4):  # one short of the backstop
        _insert(web_conn)

    assert _row_count_at(web_conn, old_ts) == 1


# -- EventThrottle -------------------------------------------------------------


def test_event_throttle_collapses_addresses_in_one_prefix():
    """Keys are always an address prefix -- two addresses inside one /64 are
    the same key and share the same one-per-minute budget."""
    et = authlog.EventThrottle(interval_seconds=60.0)
    key_a = f"blocked:{clientip.ip_prefix('2001:db8::1')}"
    key_b = f"blocked:{clientip.ip_prefix('2001:db8::2')}"
    assert key_a == key_b

    assert et.allow(key_a) is True
    assert et.allow(key_b) is False  # same prefix, inside the interval


def test_event_throttle_allows_again_after_the_interval():
    clock = {"t": 0.0}
    et = authlog.EventThrottle(interval_seconds=60.0, clock=lambda: clock["t"])
    key = "blocked:203.0.113.0/24"

    assert et.allow(key) is True
    assert et.allow(key) is False
    clock["t"] = 60.0
    assert et.allow(key) is True


def test_event_throttle_is_bounded_at_max_tracked():
    et = authlog.EventThrottle(interval_seconds=3600.0, max_tracked=4)
    for i in range(20):
        et.allow(f"blocked:203.0.113.{i}/32")
    assert len(et._last) <= 4


def test_configure_replaces_blocked_log_so_the_window_does_not_leak():
    """R19: two successive configure() calls must give a fresh blocked_log, or
    an 'at most one row per minute' assertion becomes order-dependent across
    the tests that build several apps in one process."""
    key = "blocked:203.0.113.0/24"

    authlog.configure(enabled=True, max_age_days=90, max_rows=5000)
    assert authlog.blocked_log.allow(key) is True
    assert authlog.blocked_log.allow(key) is False

    authlog.configure(enabled=True, max_age_days=90, max_rows=5000)
    assert authlog.blocked_log.allow(key) is True  # fresh throttle, not the armed one


# -- D8: thread safety --------------------------------------------------------


def test_record_attempt_is_thread_safe(tmp_path):
    """record_attempt runs on the event loop for some callers and in an AnyIO
    worker for others, so _state_lock's dict/int arithmetic has to survive
    concurrent callers without raising -- the dictionary-changed-size failure
    D8 exists to prevent."""
    db_path = tmp_path / "synopticon.db"
    store.connect(db_path).close()  # create + migrate once

    errors_seen: list[BaseException] = []
    stop = threading.Event()

    def worker():
        conn = store.connect(db_path)
        try:
            while not stop.is_set():
                try:
                    authlog.record_attempt(
                        conn,
                        event="login",
                        outcome="failure",
                        username="attacker",
                        ip="203.0.113.9",
                    )
                except BaseException as exc:  # noqa: BLE001
                    errors_seen.append(exc)
        finally:
            conn.close()

    def configurer():
        while not stop.is_set():
            authlog.configure(enabled=True, max_age_days=90, max_rows=5000)
            authlog.retention_policy()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    threads.append(threading.Thread(target=configurer))
    for t in threads:
        t.start()
    time.sleep(0.5)
    stop.set()
    for t in threads:
        t.join(timeout=5)

    assert errors_seen == []


# -- auth_log / log_summary ----------------------------------------------------


def test_auth_log_pagination_and_filters(web_conn):
    _insert(web_conn, username="alice", ip="203.0.113.1")
    _insert(web_conn, username="bob", outcome="failure", ip="203.0.113.2")
    _insert(web_conn, username="alice", outcome="failure", ip="203.0.113.1")

    items, total = authlog.auth_log(web_conn, limit=2, offset=0)
    assert total == 3
    assert len(items) == 2
    assert items[0]["id"] > items[1]["id"]  # newest first

    items, total = authlog.auth_log(web_conn, username="alice")
    assert total == 2
    assert all(i["username"] == "alice" for i in items)

    items, total = authlog.auth_log(web_conn, ip="203.0.113.2")
    assert total == 1
    assert items[0]["username"] == "bob"


def test_auth_log_failure_outcome_includes_pending_password_bad(web_conn):
    _insert(web_conn, outcome="failure", username="a")
    _insert(web_conn, outcome="pending", reason="password_bad", username="b")
    _insert(web_conn, outcome="pending", reason="password_ok", username="c")

    items, total = authlog.auth_log(web_conn, outcome="failure")
    assert total == 2
    assert {i["username"] for i in items} == {"a", "b"}


def test_log_summary_widened_failed_predicate(web_conn):
    since = int(time.time()) - 60
    _insert(web_conn, outcome="failure", username="a", ip="203.0.113.1")
    _insert(web_conn, outcome="pending", reason="password_bad", username="b", ip="203.0.113.2")
    _insert(web_conn, outcome="pending", reason="password_ok", username="c", ip="203.0.113.1")
    _insert(web_conn, outcome="success", username="a", ip="203.0.113.1")

    summary = authlog.log_summary(web_conn, since)
    assert summary["total"] == 4
    assert summary["failed"] == 2  # 'failure' plus 'pending'/'password_bad' only
    assert summary["distinct_ips"] == 2
    assert summary["distinct_usernames"] == 3


# -- prune_auth_log -------------------------------------------------------------


def test_prune_auth_log_ages_out_old_rows(web_conn):
    old_ts = int(time.time()) - 200 * 86400
    new_ts = int(time.time())
    web_conn.execute(
        "INSERT INTO web_auth_log (ts, event, outcome) VALUES (?, 'login', 'success')",
        (old_ts,),
    )
    web_conn.execute(
        "INSERT INTO web_auth_log (ts, event, outcome) VALUES (?, 'login', 'success')",
        (new_ts,),
    )
    web_conn.commit()

    removed = authlog.prune_auth_log(web_conn, max_age_days=90, max_rows=0)
    assert removed == 1
    assert _count(web_conn) == 1


def test_prune_auth_log_row_cap(web_conn):
    for _ in range(10):
        _insert(web_conn)

    removed = authlog.prune_auth_log(web_conn, max_age_days=0, max_rows=5)
    assert removed == 5
    assert _count(web_conn) == 5


def test_prune_auth_log_bounds_can_be_disabled(web_conn):
    old_ts = int(time.time()) - 200 * 86400
    web_conn.execute(
        "INSERT INTO web_auth_log (ts, event, outcome) VALUES (?, 'login', 'success')",
        (old_ts,),
    )
    web_conn.commit()

    removed = authlog.prune_auth_log(web_conn, max_age_days=0, max_rows=0)
    assert removed == 0
    assert _count(web_conn) == 1


# -- scheduler housekeeping (section 4.6, R18) ---------------------------------


def test_scheduler_housekeeping_gates_prune_on_retention_only(tmp_path, monkeypatch):
    """The scheduler must purge sessions and login challenges unconditionally,
    and prune the sign-in log only when authlog says logging is enabled --
    turning the log off promises the rows already there are kept."""
    from synopticon.web import scheduler as scheduler_mod

    calls: list[str] = []
    monkeypatch.setattr(sessions, "purge_expired", lambda c: calls.append("sessions") or 0)
    monkeypatch.setattr(
        twofactor, "purge_expired_challenges", lambda c: calls.append("challenges") or 0
    )
    monkeypatch.setattr(authlog, "prune_auth_log", lambda c, **kw: calls.append("log") or 0)

    db_path = tmp_path / "synopticon.db"
    store.connect(db_path).close()
    sched = scheduler_mod.Scheduler(lambda: store.connect(db_path), job_manager=None)

    authlog.configure(enabled=False, max_age_days=90, max_rows=5000)
    sched._housekeeping()
    assert calls == ["sessions", "challenges"]

    calls.clear()
    authlog.configure(enabled=True, max_age_days=90, max_rows=5000)
    sched._housekeeping()
    assert calls == ["sessions", "challenges", "log"]


def test_scheduler_housekeeping_one_failing_pass_does_not_block_the_others(
    tmp_path, monkeypatch
):
    """Each of the three passes has its own try/except -- a broken session
    purge must not stop the login-challenge purge or the log prune."""
    from synopticon.web import scheduler as scheduler_mod

    calls: list[str] = []

    def _broken(conn):
        raise RuntimeError("boom")

    monkeypatch.setattr(sessions, "purge_expired", _broken)
    monkeypatch.setattr(
        twofactor, "purge_expired_challenges", lambda c: calls.append("challenges") or 0
    )
    monkeypatch.setattr(authlog, "prune_auth_log", lambda c, **kw: calls.append("log") or 0)

    db_path = tmp_path / "synopticon.db"
    store.connect(db_path).close()
    sched = scheduler_mod.Scheduler(lambda: store.connect(db_path), job_manager=None)

    authlog.configure(enabled=True, max_age_days=90, max_rows=5000)
    sched._housekeeping()  # must not raise despite the broken session purge

    assert calls == ["challenges", "log"]


# -- section 4.7: never a credential -------------------------------------------


def test_record_attempt_signature_cannot_carry_a_credential():
    """record_attempt has no parameter through which a password, session
    token, token hash, API key or key prefix could ever reach a row -- the
    structural half of section 4.7's prohibition."""
    params = set(inspect.signature(authlog.record_attempt).parameters)
    assert params == {
        "conn",
        "event",
        "outcome",
        "reason",
        "username",
        "user_id",
        "ip",
        "user_agent",
    }
    forbidden = {
        "password",
        "token",
        "token_hash",
        "session_token",
        "api_key",
        "key",
        "key_prefix",
        "secret",
        "code",
        "recovery_code",
    }
    assert not (forbidden & params)


def test_web_auth_log_schema_has_no_credential_columns(web_conn):
    """The schema half of the same prohibition: there is no column a future
    call site could even be tempted to fill with one."""
    cols = {r["name"] for r in web_conn.execute("PRAGMA table_info(web_auth_log)")}
    forbidden = {
        "password",
        "password_hash",
        "token",
        "token_hash",
        "session_token",
        "api_key",
        "key_hash",
        "key_prefix",
        "secret",
        "code",
        "recovery_code",
    }
    assert not (forbidden & cols)
