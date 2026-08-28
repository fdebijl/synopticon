"""web.totp (RFC 6238) and web.auth.twofactor: two-step sign-in (SEC1)."""

from __future__ import annotations

import base64

import pytest

from synopticon.db import store
from synopticon.web import auth, totp
from synopticon.web.auth import twofactor


def _user(conn, name="alice"):
    return auth.create_user(conn, name, "hunter22")


# -- web/totp.py: RFC 6238 vectors and the provisioning URI -------------------


# RFC 6238 Appendix B, SHA-1 column, 8-digit codes. The secret is the ASCII
# string "12345678901234567890" (20 bytes), which our functions take base32
# encoded like every other secret in this module.
_RFC6238_SECRET = base64.b32encode(b"12345678901234567890").decode("ascii").rstrip("=")
_RFC6238_VECTORS = [
    (59, "94287082"),
    (1111111109, "07081804"),
    (1111111111, "14050471"),
    (1234567890, "89005924"),
    (2000000000, "69279037"),
    (20000000000, "65353130"),
]


@pytest.mark.parametrize("timestamp,expected", _RFC6238_VECTORS)
def test_code_for_matches_rfc6238_vectors(timestamp, expected):
    step = timestamp // 30
    assert totp.code_for(_RFC6238_SECRET, step, digits=8) == expected


def test_verify_matches_rfc6238_vector_at_default_digits():
    # The 6-digit code is the last 6 of the 8-digit RFC vector's HOTP output
    # only by coincidence of truncation math, so derive it the same way instead
    # of hand-computing a second literal.
    step = 59 // 30
    code6 = totp.code_for(_RFC6238_SECRET, step, digits=6)
    assert totp.verify(_RFC6238_SECRET, code6, now=59, skew=0) == step


def test_provisioning_uri_literal_expected_string_including_space_in_issuer():
    uri = totp.provisioning_uri(
        "JBSWY3DPEHPK3PXP", account="alice", issuer="Synopticon Home"
    )
    assert uri == (
        "otpauth://totp/Synopticon%20Home:alice"
        "?secret=JBSWY3DPEHPK3PXP&issuer=Synopticon%20Home"
        "&algorithm=SHA1&digits=6&period=30"
    )


def test_normalize_code_strips_whitespace_and_punctuation():
    assert totp.normalize_code(" 123 456 ") == "123456"
    assert totp.normalize_code("123-456") == "123456"


def test_verify_rejects_wrong_length_or_non_digit():
    secret = totp.generate_secret()
    assert totp.verify(secret, "12345", now=1000) is None
    assert totp.verify(secret, "abcdef", now=1000) is None


def test_verify_never_short_circuits_and_finds_match_within_skew():
    secret = totp.generate_secret()
    step = totp.current_step(1000)
    code = totp.code_for(secret, step + 1)  # one step ahead, inside skew=1
    assert totp.verify(secret, code, now=1000, skew=1) == step + 1


# -- twofactor.py: enrolment --------------------------------------------------


def test_enrolment_round_trip(web_conn):
    uid = _user(web_conn)
    pending = twofactor.start_totp_enrolment(web_conn, uid, now=1000)
    assert pending.pending_expires_in == twofactor.ENROLMENT_TTL

    status = twofactor.totp_status(web_conn, uid, now=1000)
    assert status["pending"] is True
    assert status["enrolled"] is False

    code = totp.code_for(pending.secret, totp.current_step(1000))
    codes = twofactor.confirm_totp_enrolment(web_conn, uid, code, now=1000)
    assert codes is not None
    assert len(codes) == 10

    assert twofactor.totp_enabled(web_conn, uid) is True
    status = twofactor.totp_status(web_conn, uid, now=1000)
    assert status["enrolled"] is True
    assert status["pending"] is False
    assert status["recovery_remaining"] == 10


def test_start_totp_enrolment_reuses_pending_secret_within_ttl(web_conn):
    uid = _user(web_conn)
    first = twofactor.start_totp_enrolment(web_conn, uid, now=1000)
    second = twofactor.start_totp_enrolment(web_conn, uid, now=1100)
    assert second.secret == first.secret
    # Reuse still reports the remaining budget against the ORIGINAL creation.
    assert second.pending_expires_in == twofactor.ENROLMENT_TTL - 100


def test_start_totp_enrolment_fresh_mints_a_new_secret(web_conn):
    uid = _user(web_conn)
    first = twofactor.start_totp_enrolment(web_conn, uid, now=1000)
    second = twofactor.start_totp_enrolment(web_conn, uid, fresh=True, now=1100)
    assert second.secret != first.secret
    assert second.pending_expires_in == twofactor.ENROLMENT_TTL


def test_start_totp_enrolment_replaces_a_stale_pending_secret(web_conn):
    uid = _user(web_conn)
    first = twofactor.start_totp_enrolment(web_conn, uid, now=1000)
    later = 1000 + twofactor.ENROLMENT_TTL + 1
    second = twofactor.start_totp_enrolment(web_conn, uid, now=later)
    assert second.secret != first.secret


def test_start_totp_enrolment_raises_when_already_confirmed(web_conn):
    uid = _user(web_conn)
    pending = twofactor.start_totp_enrolment(web_conn, uid, now=1000)
    code = totp.code_for(pending.secret, totp.current_step(1000))
    twofactor.confirm_totp_enrolment(web_conn, uid, code, now=1000)

    with pytest.raises(twofactor.TotpAlreadyEnrolledError):
        twofactor.start_totp_enrolment(web_conn, uid, now=1000)


def test_confirm_totp_enrolment_raises_with_no_pending_row(web_conn):
    uid = _user(web_conn)
    with pytest.raises(twofactor.NoPendingEnrolmentError):
        twofactor.confirm_totp_enrolment(web_conn, uid, "000000", now=1000)


def test_confirm_totp_enrolment_raises_not_returns_none_when_pending_is_stale(web_conn):
    """A pending row older than ENROLMENT_TTL is treated as absent -- NEVER
    confirmed, and the caller must see a 409 (start again), not a 401 (check
    your phone's clock), which names the wrong problem."""
    uid = _user(web_conn)
    pending = twofactor.start_totp_enrolment(web_conn, uid, now=1000)
    stale_now = 1000 + twofactor.ENROLMENT_TTL + 1
    code = totp.code_for(pending.secret, totp.current_step(stale_now))

    with pytest.raises(twofactor.NoPendingEnrolmentError):
        twofactor.confirm_totp_enrolment(web_conn, uid, code, now=stale_now)


def test_confirm_totp_enrolment_returns_none_for_bad_code_pending_untouched(web_conn):
    uid = _user(web_conn)
    twofactor.start_totp_enrolment(web_conn, uid, now=1000)
    assert twofactor.confirm_totp_enrolment(web_conn, uid, "000000", now=1000) is None
    status = twofactor.totp_status(web_conn, uid, now=1000)
    assert status["pending"] is True  # still there, unconfirmed


def test_challenge_required_matches_the_correction(web_conn):
    enrolled = _user(web_conn, "enrolled")
    pending = twofactor.start_totp_enrolment(web_conn, enrolled, now=1000)
    code = totp.code_for(pending.secret, totp.current_step(1000))
    twofactor.confirm_totp_enrolment(web_conn, enrolled, code, now=1000)

    bare = _user(web_conn, "bare")

    # Enrolled account, right or wrong password -> challenge either way.
    assert twofactor.challenge_required(web_conn, "enrolled", enrolled) is True
    # Unknown username / wrong password anywhere -> looks like the enrolled one.
    assert twofactor.challenge_required(web_conn, "enrolled", None) is True
    assert twofactor.challenge_required(web_conn, "nobody", None) is True
    # A non-enrolled account signs in in one step.
    assert twofactor.challenge_required(web_conn, "bare", bare) is False


def test_disable_totp_is_idempotent_and_drops_recovery_codes(web_conn):
    uid = _user(web_conn)
    pending = twofactor.start_totp_enrolment(web_conn, uid, now=1000)
    code = totp.code_for(pending.secret, totp.current_step(1000))
    twofactor.confirm_totp_enrolment(web_conn, uid, code, now=1000)

    twofactor.disable_totp(web_conn, uid)
    assert twofactor.totp_enabled(web_conn, uid) is False
    assert twofactor.count_recovery_codes(web_conn, uid) == 0

    twofactor.disable_totp(web_conn, uid)  # idempotent: no row, no error


# -- twofactor.py: verify_totp replay guard -----------------------------------


def test_verify_totp_same_code_two_connections_accepts_exactly_once(tmp_path):
    db_path = tmp_path / "synopticon.db"
    setup = store.connect(db_path)
    uid = _user(setup)
    pending = twofactor.start_totp_enrolment(setup, uid, now=1000)
    # Confirmation itself stamps last_step for its own code, so the code under
    # test here has to be a later, distinct step -- otherwise the replay guard
    # would already have consumed it before either connection gets a turn.
    now = 1000 + 30
    code = totp.code_for(pending.secret, totp.current_step(now))
    twofactor.confirm_totp_enrolment(
        setup, uid, totp.code_for(pending.secret, totp.current_step(1000)), now=1000
    )
    setup.close()

    conn_a = store.connect(db_path)
    conn_b = store.connect(db_path)
    try:
        first = twofactor.verify_totp(conn_a, uid, code, now=now)
        second = twofactor.verify_totp(conn_b, uid, code, now=now)
        assert first is True
        assert second is False  # the same step was already spent
    finally:
        conn_a.close()
        conn_b.close()


def test_verify_totp_clock_rewind_escape_overrides_a_last_step_in_the_future(web_conn):
    uid = _user(web_conn)
    pending = twofactor.start_totp_enrolment(web_conn, uid, now=1000)
    code = totp.code_for(pending.secret, totp.current_step(1000))
    twofactor.confirm_totp_enrolment(web_conn, uid, code, now=1000)

    # Simulate a fast RTC: last_step recorded 300 steps ahead of where the
    # server's clock now is (300 > _CLOCK_REWIND_STEPS's 240 would NOT be
    # covered -- use exactly inside the escape window).
    future_step = totp.current_step(1000) + 300
    web_conn.execute(
        "UPDATE web_totp SET last_step = ? WHERE user_id = ?", (future_step, uid)
    )
    web_conn.commit()

    later_now = 1000 + 60  # two steps on, still within the 240-step rewind escape
    later_code = totp.code_for(pending.secret, totp.current_step(later_now))
    assert twofactor.verify_totp(web_conn, uid, later_code, now=later_now) is True


def test_verify_totp_false_for_unconfirmed_or_unknown_user(web_conn):
    uid = _user(web_conn)
    twofactor.start_totp_enrolment(web_conn, uid, now=1000)  # pending, not confirmed
    assert twofactor.verify_totp(web_conn, uid, "000000", now=1000) is False
    assert twofactor.verify_totp(web_conn, 99999, "000000", now=1000) is False


# -- recovery codes ------------------------------------------------------------


def test_recovery_codes_are_single_use(web_conn):
    uid = _user(web_conn)
    codes = twofactor.generate_recovery_codes(web_conn, uid, count=3)
    assert len(codes) == 3
    assert twofactor.count_recovery_codes(web_conn, uid) == 3

    first = codes[0]
    assert twofactor.consume_recovery_code(web_conn, uid, first) is True
    assert twofactor.count_recovery_codes(web_conn, uid) == 2
    assert twofactor.consume_recovery_code(web_conn, uid, first) is False  # spent


def test_recovery_codes_accept_hyphenated_or_bare_form(web_conn):
    uid = _user(web_conn)
    codes = twofactor.generate_recovery_codes(web_conn, uid, count=1)
    hyphenated = codes[0]
    assert "-" in hyphenated
    bare = hyphenated.replace("-", "")
    assert twofactor.consume_recovery_code(web_conn, uid, bare) is True


def test_generate_recovery_codes_replaces_the_old_set(web_conn):
    uid = _user(web_conn)
    first = set(twofactor.generate_recovery_codes(web_conn, uid, count=5))
    second = set(twofactor.generate_recovery_codes(web_conn, uid, count=5))
    assert twofactor.count_recovery_codes(web_conn, uid) == 5
    assert first.isdisjoint(second)
    for code in first:
        assert twofactor.consume_recovery_code(web_conn, uid, code) is False


# -- login challenges ----------------------------------------------------------


def test_take_login_challenge_allows_exactly_max_attempts(web_conn):
    token = twofactor.start_login_challenge(web_conn, "alice", None, ttl_seconds=300)
    for _ in range(5):
        assert twofactor.take_login_challenge(web_conn, token, max_attempts=5) == ("alice", None)
    assert twofactor.take_login_challenge(web_conn, token, max_attempts=5) is None


def test_peek_login_challenge_does_not_consume_an_attempt(web_conn):
    uid = _user(web_conn)
    token = twofactor.start_login_challenge(web_conn, "alice", uid, ttl_seconds=300)
    assert twofactor.peek_login_challenge(web_conn, token) == ("alice", uid)
    assert twofactor.peek_login_challenge(web_conn, token) == ("alice", uid)
    # Peeking spent nothing: all 5 real attempts are still available.
    for _ in range(5):
        assert twofactor.take_login_challenge(web_conn, token, max_attempts=5) == ("alice", uid)
    assert twofactor.take_login_challenge(web_conn, token, max_attempts=5) is None


def test_peek_and_take_return_none_for_unknown_or_expired(web_conn):
    assert twofactor.peek_login_challenge(web_conn, "no-such-token") is None
    assert twofactor.take_login_challenge(web_conn, "no-such-token") is None

    token = twofactor.start_login_challenge(web_conn, "alice", None, ttl_seconds=-1)
    assert twofactor.peek_login_challenge(web_conn, token) is None
    assert twofactor.take_login_challenge(web_conn, token) is None


def test_delete_and_purge_login_challenges(web_conn):
    alice = _user(web_conn, "alice")
    bob = _user(web_conn, "bob")

    token = twofactor.start_login_challenge(web_conn, "alice", alice, ttl_seconds=300)
    twofactor.delete_login_challenge(web_conn, token)
    assert twofactor.peek_login_challenge(web_conn, token) is None

    token2 = twofactor.start_login_challenge(web_conn, "bob", bob, ttl_seconds=300)
    assert twofactor.purge_user_challenges(web_conn, bob) == 1
    assert twofactor.peek_login_challenge(web_conn, token2) is None


def test_purge_expired_challenges_removes_only_expired(web_conn):
    live = twofactor.start_login_challenge(web_conn, "alice", None, ttl_seconds=300)
    expired = twofactor.start_login_challenge(web_conn, "bob", None, ttl_seconds=-1)
    n = twofactor.purge_expired_challenges(web_conn)
    assert n == 1
    assert twofactor.peek_login_challenge(web_conn, live) is not None
    assert twofactor.peek_login_challenge(web_conn, expired) is None


def test_start_login_challenge_stores_truncated_username(web_conn):
    from synopticon.web.clientip import USERNAME_MAX

    huge = "x" * (USERNAME_MAX + 500)
    twofactor.start_login_challenge(web_conn, huge, None, ttl_seconds=300)
    row = web_conn.execute("SELECT username FROM web_login_challenges").fetchone()
    assert len(row["username"]) == USERNAME_MAX


def test_start_login_challenge_evicts_down_to_the_row_cap(web_conn, monkeypatch):
    """§4.4/F5: the row cap is enforced with a dialect-safe DELETE ... WHERE
    token_hash IN (SELECT ... ORDER BY ... LIMIT ?) -- never the SQLite-only
    `DELETE ... ORDER BY ... LIMIT` form, which fails outright on PostgreSQL."""
    monkeypatch.setattr(twofactor, "_CHALLENGE_MAX_ROWS", 5)
    for i in range(8):
        twofactor.start_login_challenge(web_conn, f"user{i}", None, ttl_seconds=300)
    n = web_conn.execute("SELECT COUNT(*) AS n FROM web_login_challenges").fetchone()["n"]
    assert n == 5


def test_start_login_challenge_never_emits_the_sqlite_only_delete_order_by_limit_form():
    """Statement-text guard for F5: the SQLite-only `DELETE ... ORDER BY ...
    LIMIT` form must never appear -- it passes through db/dialect.py's
    PostgreSQL translation untouched and fails at execution there."""
    import inspect

    source = inspect.getsource(twofactor.start_login_challenge)
    assert "DELETE FROM web_login_challenges WHERE token_hash IN (" in source
    assert "DELETE FROM web_login_challenges ORDER BY" not in source
    assert "DELETE FROM web_login_challenges WHERE expires_at <= ? ORDER BY" not in source
