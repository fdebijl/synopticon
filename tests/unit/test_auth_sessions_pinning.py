"""web.auth.sessions: session pinning (SEC4)."""

from __future__ import annotations

import pytest

from synopticon.web import auth
from synopticon.web.auth import sessions
from synopticon.web.clientip import ClientFacts


def _user(conn, name="alice"):
    return auth.create_user(conn, name, "hunter22")


CHROME = ClientFacts(user_agent="Mozilla/5.0 Chrome/120.0.0.0", ip="192.168.1.5")
CHROME_NEXT_VERSION = ClientFacts(user_agent="Mozilla/5.0 Chrome/121.0.0.0", ip="192.168.1.5")
CHROME_OTHER_SUBNET = ClientFacts(user_agent="Mozilla/5.0 Chrome/120.0.0.0", ip="10.0.0.9")
FIREFOX = ClientFacts(user_agent="Mozilla/5.0 Firefox/119.0", ip="192.168.1.5")


# -- fingerprint ---------------------------------------------------------------


def test_fingerprint_none_for_off_unknown_or_no_client():
    assert sessions.fingerprint(sessions.PIN_OFF, CHROME) is None
    assert sessions.fingerprint("bogus-mode", CHROME) is None
    assert sessions.fingerprint(sessions.PIN_DEVICE, None) is None


def test_fingerprint_device_mode_ignores_ip_change():
    a = sessions.fingerprint(sessions.PIN_DEVICE, CHROME)
    b = sessions.fingerprint(sessions.PIN_DEVICE, CHROME_OTHER_SUBNET)
    assert a is not None
    assert a == b


def test_fingerprint_device_mode_survives_ua_version_bump():
    a = sessions.fingerprint(sessions.PIN_DEVICE, CHROME)
    b = sessions.fingerprint(sessions.PIN_DEVICE, CHROME_NEXT_VERSION)
    assert a == b


def test_fingerprint_device_network_mode_is_sensitive_to_subnet():
    a = sessions.fingerprint(sessions.PIN_DEVICE_NETWORK, CHROME)
    b = sessions.fingerprint(sessions.PIN_DEVICE_NETWORK, CHROME_OTHER_SUBNET)
    assert a != b


def test_fingerprint_different_modes_never_collide():
    device = sessions.fingerprint(sessions.PIN_DEVICE, CHROME)
    device_network = sessions.fingerprint(sessions.PIN_DEVICE_NETWORK, CHROME)
    assert device != device_network


def test_fingerprint_different_browser_differs():
    a = sessions.fingerprint(sessions.PIN_DEVICE, CHROME)
    b = sessions.fingerprint(sessions.PIN_DEVICE, FIREFOX)
    assert a != b


# -- create_session / validate_session -----------------------------------------


def test_create_session_unpinned_by_default(web_conn):
    uid = _user(web_conn)
    token = sessions.create_session(web_conn, uid)
    assert sessions.validate_session(web_conn, token) == uid
    assert sessions.validate_session(web_conn, token, client=CHROME) == uid
    row = web_conn.execute(
        "SELECT pin_mode, pin_hash FROM web_sessions WHERE user_id = ?", (uid,)
    ).fetchone()
    assert row["pin_mode"] is None
    assert row["pin_hash"] is None


def test_create_session_stamps_pin_when_mode_set_and_client_given(web_conn):
    uid = _user(web_conn)
    sessions.set_pin_mode(web_conn, uid, sessions.PIN_DEVICE, client=CHROME)
    token = sessions.create_session(web_conn, uid, client=CHROME)
    row = web_conn.execute(
        "SELECT pin_mode, pin_hash FROM web_sessions WHERE user_id = ?", (uid,)
    ).fetchone()
    assert row["pin_mode"] == sessions.PIN_DEVICE
    assert row["pin_hash"] == sessions.fingerprint(sessions.PIN_DEVICE, CHROME)
    assert sessions.validate_session(web_conn, token, client=CHROME) == uid


def test_create_session_leaves_unpinned_when_mode_set_but_no_client(web_conn):
    uid = _user(web_conn)
    sessions.set_pin_mode(web_conn, uid, sessions.PIN_DEVICE, client=CHROME)
    token = sessions.create_session(web_conn, uid)
    row = web_conn.execute(
        "SELECT pin_mode FROM web_sessions WHERE user_id = ?", (uid,)
    ).fetchone()
    assert row["pin_mode"] is None
    assert sessions.validate_session(web_conn, token) == uid


def test_validate_session_expiry_checked_before_pin(web_conn):
    """An expired, pinned session is reaped -- never a SessionPinViolation."""
    uid = _user(web_conn)
    sessions.set_pin_mode(web_conn, uid, sessions.PIN_DEVICE, client=CHROME)
    token = sessions.create_session(web_conn, uid, ttl_days=30, client=CHROME)
    web_conn.execute(
        "UPDATE web_sessions SET expires_at = 0 WHERE token_hash = ?",
        (auth._sha256_hex(token),),
    )
    web_conn.commit()
    # A mismatched client would normally raise; expiry must win first.
    assert sessions.validate_session(web_conn, token, client=FIREFOX) is None


def test_validate_session_mismatch_raises_and_destroys_row(web_conn):
    uid = _user(web_conn)
    sessions.set_pin_mode(web_conn, uid, sessions.PIN_DEVICE, client=CHROME)
    token = sessions.create_session(web_conn, uid, client=CHROME)

    with pytest.raises(sessions.SessionPinViolation) as exc_info:
        sessions.validate_session(web_conn, token, client=FIREFOX)
    assert exc_info.value.destroyed is True

    row = web_conn.execute(
        "SELECT 1 FROM web_sessions WHERE token_hash = ?", (auth._sha256_hex(token),)
    ).fetchone()
    assert row is None


def test_validate_session_no_client_against_pinned_row_leaves_row_in_place(web_conn):
    """A `client=None` violation must never delete the session (no way back)."""
    uid = _user(web_conn)
    sessions.set_pin_mode(web_conn, uid, sessions.PIN_DEVICE, client=CHROME)
    token = sessions.create_session(web_conn, uid, client=CHROME)

    with pytest.raises(sessions.SessionPinViolation) as exc_info:
        sessions.validate_session(web_conn, token)
    assert exc_info.value.destroyed is False

    row = web_conn.execute(
        "SELECT 1 FROM web_sessions WHERE token_hash = ?", (auth._sha256_hex(token),)
    ).fetchone()
    assert row is not None
    # And the session is still perfectly valid for the client it was pinned to.
    assert sessions.validate_session(web_conn, token, client=CHROME) == uid


# -- get_pin_mode / set_pin_mode -------------------------------------------------


def test_get_pin_mode_defaults_off(web_conn):
    uid = _user(web_conn)
    assert sessions.get_pin_mode(web_conn, uid) == sessions.PIN_OFF


def test_get_pin_mode_unknown_user_is_off(web_conn):
    assert sessions.get_pin_mode(web_conn, 999999) == sessions.PIN_OFF


def test_set_pin_mode_unknown_mode_raises(web_conn):
    uid = _user(web_conn)
    with pytest.raises(ValueError):
        sessions.set_pin_mode(web_conn, uid, "sometimes", client=CHROME)


def test_set_pin_mode_device_with_no_client_raises(web_conn):
    uid = _user(web_conn)
    with pytest.raises(ValueError):
        sessions.set_pin_mode(web_conn, uid, sessions.PIN_DEVICE, client=None)


def test_set_pin_mode_off_with_no_client_is_fine(web_conn):
    uid = _user(web_conn)
    # Should not raise: turning pinning off never needs client facts.
    sessions.set_pin_mode(web_conn, uid, sessions.PIN_OFF, client=None)
    assert sessions.get_pin_mode(web_conn, uid) == sessions.PIN_OFF


def test_set_pin_mode_off_keep_token_none_unpins_in_place_deletes_none(web_conn):
    """R6: the CLI's whole contract. `session-pin off` must not sign anyone out."""
    uid = _user(web_conn)
    sessions.set_pin_mode(web_conn, uid, sessions.PIN_DEVICE, client=CHROME)
    t1 = sessions.create_session(web_conn, uid, client=CHROME)
    t2 = sessions.create_session(web_conn, uid, client=FIREFOX)

    revoked = sessions.set_pin_mode(web_conn, uid, sessions.PIN_OFF, keep_token=None, client=None)

    assert revoked == 0
    assert sessions.get_pin_mode(web_conn, uid) == sessions.PIN_OFF
    rows = web_conn.execute(
        "SELECT pin_mode, pin_hash FROM web_sessions WHERE user_id = ?", (uid,)
    ).fetchall()
    assert len(rows) == 2
    assert all(r["pin_mode"] is None and r["pin_hash"] is None for r in rows)
    # Both sessions still validate, unpinned, with no client at all.
    assert sessions.validate_session(web_conn, t1) == uid
    assert sessions.validate_session(web_conn, t2) == uid


def test_set_pin_mode_repins_keep_token_and_deletes_others(web_conn):
    uid = _user(web_conn)
    keep = sessions.create_session(web_conn, uid)
    other = sessions.create_session(web_conn, uid)

    revoked = sessions.set_pin_mode(
        web_conn, uid, sessions.PIN_DEVICE, keep_token=keep, client=CHROME
    )

    assert revoked == 1
    assert sessions.validate_session(web_conn, other) is None
    assert sessions.validate_session(web_conn, keep, client=CHROME) == uid
    with pytest.raises(sessions.SessionPinViolation):
        sessions.validate_session(web_conn, keep, client=FIREFOX)


def test_set_pin_mode_switching_to_off_with_keep_token_unpins_keep_and_deletes_rest(web_conn):
    uid = _user(web_conn)
    sessions.set_pin_mode(web_conn, uid, sessions.PIN_DEVICE, client=CHROME)
    keep = sessions.create_session(web_conn, uid, client=CHROME)
    other = sessions.create_session(web_conn, uid, client=CHROME)

    revoked = sessions.set_pin_mode(
        web_conn, uid, sessions.PIN_OFF, keep_token=keep, client=CHROME
    )

    assert revoked == 1
    assert sessions.validate_session(web_conn, other) is None
    # keep_token is unpinned in place: it now validates with no client at all.
    assert sessions.validate_session(web_conn, keep) == uid


# -- delete_user_sessions / count_user_sessions ---------------------------------


def test_delete_user_sessions_except_token_keeps_caller_cookie(web_conn):
    uid = _user(web_conn)
    keep = sessions.create_session(web_conn, uid)
    other = sessions.create_session(web_conn, uid)

    revoked = sessions.delete_user_sessions(web_conn, uid, except_token=keep)

    assert revoked == 1
    assert sessions.validate_session(web_conn, keep) == uid
    assert sessions.validate_session(web_conn, other) is None


def test_delete_user_sessions_default_revokes_everything(web_conn):
    uid = _user(web_conn)
    t1 = sessions.create_session(web_conn, uid)
    t2 = sessions.create_session(web_conn, uid)

    assert sessions.delete_user_sessions(web_conn, uid) == 2
    assert sessions.validate_session(web_conn, t1) is None
    assert sessions.validate_session(web_conn, t2) is None


def test_count_user_sessions_excludes_callers_own_token(web_conn):
    uid = _user(web_conn)
    mine = sessions.create_session(web_conn, uid)
    sessions.create_session(web_conn, uid)
    sessions.create_session(web_conn, uid)

    assert sessions.count_user_sessions(web_conn, uid) == 3
    assert sessions.count_user_sessions(web_conn, uid, except_token=mine) == 2


def test_count_user_sessions_excludes_expired(web_conn):
    uid = _user(web_conn)
    live = sessions.create_session(web_conn, uid)
    expired = sessions.create_session(web_conn, uid)
    web_conn.execute(
        "UPDATE web_sessions SET expires_at = 0 WHERE token_hash = ?",
        (auth._sha256_hex(expired),),
    )
    web_conn.commit()

    assert sessions.count_user_sessions(web_conn, uid) == 1
    assert sessions.validate_session(web_conn, live) == uid


# -- cache_key / cache_prefix ----------------------------------------------------


def test_cache_key_differs_by_client(web_conn):
    token = "sometoken"
    assert sessions.cache_key(token, CHROME) != sessions.cache_key(token, FIREFOX)


def test_cache_key_stable_across_ua_version_bump():
    token = "sometoken"
    assert sessions.cache_key(token, CHROME) == sessions.cache_key(token, CHROME_NEXT_VERSION)


def test_cache_key_starts_with_cache_prefix():
    token = "sometoken"
    assert sessions.cache_key(token, CHROME).startswith(sessions.cache_prefix(token))
    assert sessions.cache_key(token, None).startswith(sessions.cache_prefix(token))


def test_cache_prefix_differs_per_token():
    assert sessions.cache_prefix("a") != sessions.cache_prefix("b")
