"""web.configio: the network allowlist's consumers (SEC2) -- PUT /api/config's
proxy-trust + self-lockout guards (guarded_write), and the allowlist gate that
refuses a request before it reaches a route.

Named tests here pin the corrections the security contract calls out
explicitly (D5, F7, R7, R8) so a regression is loud.
"""

from __future__ import annotations

import inspect
import sys

import pytest
from starlette.testclient import TestClient

from synopticon.config import load_settings
from synopticon.web import clientip, configio


def _settings(tmp_path, **security):
    return load_settings(
        storage={"data_dir": tmp_path},
        nas={"url": "https://nas.test", "account": "svc", "password": "pw"},
        security=security,
    )


# -- D5: IPAllowlist.allows takes exactly one parameter ----------------------


def test_ip_allowlist_allows_takes_exactly_one_parameter():
    sig = inspect.signature(clientip.IPAllowlist.allows)
    params = [name for name in sig.parameters if name != "self"]
    assert params == ["ip"]
    allowlist = clientip.IPAllowlist(["192.168.1.0/24"])
    with pytest.raises(TypeError):
        allowlist.allows("192.168.1.5", local_request=True)  # type: ignore[call-arg]


def test_ip_allowlist_allows_loopback_unconditionally_even_when_active():
    # RULE 4: the anti-lockout guarantee -- loopback is never refused, however
    # the list is configured.
    allowlist = clientip.IPAllowlist(["203.0.113.0/24"], allow_private=False)
    assert allowlist.active is True
    assert allowlist.allows("127.0.0.1") is True
    assert allowlist.allows("198.51.100.9") is False  # outside the list, refused


# -- guarded_write: type errors surface as 422, never 500/409 (R7) -----------


def test_guarded_write_wrong_type_returns_422_not_409_or_500(tmp_path):
    settings = _settings(tmp_path)
    errors, conflict = configio.guarded_write(
        settings,
        {"security": {"allow_from": "192.168.1.0/24"}},  # a string, not a list
        peer="127.0.0.1",
        forwarded_for="",
        bind_host=None,
        allow_lockout=False,
    )
    assert conflict is None
    assert errors is not None
    assert any(e["loc"] == "security.allow_from" for e in errors)


# -- guarded_write reads the FILE, not the captured Settings object (R7) -----


def test_guarded_write_second_save_sees_first_saves_allow_from(tmp_path):
    # `settings` is never reloaded -- exactly like the object `create_app`
    # captures at start-up -- so its own `.security.allow_from` stays `[]` for
    # both calls. The second guard must still see what the FIRST call wrote to
    # disk, or the two-step admin flow (save the list, come back and tighten
    # allow_private_networks) sails through with no 409 and locks out on the
    # next restart.
    settings = _settings(tmp_path)
    assert settings.security.allow_from == []

    errors1, conflict1 = configio.guarded_write(
        settings,
        {"security": {"allow_from": ["192.168.1.50"]}},
        peer="192.168.1.50",
        forwarded_for="",
        bind_host=None,
        allow_lockout=False,
    )
    assert errors1 is None and conflict1 is None

    # Second save carries only allow_private_networks. From an address the
    # first save's list does not cover and that is not a private range, this
    # must 409 as a self-lockout -- which only happens if the guard merged
    # against the allow_from the FIRST call wrote, not against `settings`
    # (still `[]` in memory).
    errors2, conflict2 = configio.guarded_write(
        settings,
        {"security": {"allow_private_networks": False}},
        peer="203.0.113.9",
        forwarded_for="",
        bind_host=None,
        allow_lockout=False,
    )
    assert errors2 is None
    assert conflict2 is not None
    assert conflict2["lockout"] is True
    assert settings.security.allow_from == []  # never mutated


# -- guard 1: unenforceable / untrusted proxy, R8's deleted loopback carve-out


def test_guard1_fires_even_when_peer_is_loopback(tmp_path):
    # The earlier draft added "and the peer is not loopback", which suppressed
    # this guard on exactly the documented topology (proxy on the same host).
    settings = _settings(tmp_path)
    errors, conflict = configio.guarded_write(
        settings,
        {"security": {"allow_from": ["192.168.1.50"]}},
        peer="127.0.0.1",
        forwarded_for="203.0.113.9",
        bind_host=None,
        allow_lockout=False,
    )
    assert errors is None
    assert conflict is not None
    assert conflict["unenforceable"] is True
    assert conflict["forwarded_for_present"] is True


def test_guard1_does_not_fire_without_allow_from(tmp_path):
    # Only relevant once an allowlist is actually being turned on.
    settings = _settings(tmp_path)
    errors, conflict = configio.guarded_write(
        settings,
        {"security": {"sign_in_log": False}},
        peer="127.0.0.1",
        forwarded_for="203.0.113.9",
        bind_host=None,
        allow_lockout=False,
    )
    assert errors is None
    assert conflict is None


# -- guard 2: same-host proxy forwarding nothing -----------------------------


def test_guard2_fires_for_trusted_loopback_proxy_with_no_forwarded_header(tmp_path):
    settings = _settings(tmp_path, trusted_proxies=["127.0.0.1"])
    errors, conflict = configio.guarded_write(
        settings,
        {"security": {"allow_from": ["192.168.1.50"], "trusted_proxies": ["127.0.0.1"]}},
        peer="127.0.0.1",
        forwarded_for="",
        bind_host=None,
        allow_lockout=False,
    )
    assert errors is None
    assert conflict is not None
    assert conflict["unenforceable"] is True


def test_guard2_backstop_fires_when_bind_host_is_loopback(tmp_path):
    # Nobody is sending the header at all: trusted_proxies is empty, the peer
    # is loopback, and the process is bound to loopback -- the address list is
    # either meaningless or unenforceable.
    settings = _settings(tmp_path)
    errors, conflict = configio.guarded_write(
        settings,
        {"security": {"allow_from": ["192.168.1.50"]}},
        peer="127.0.0.1",
        forwarded_for="",
        bind_host="127.0.0.1",
        allow_lockout=False,
    )
    assert errors is None
    assert conflict is not None
    assert conflict["unenforceable"] is True


# -- guard 0: environment shadow, NEVER overridable by allow_lockout (F7) ----


def test_guard0_fires_when_env_shadows_allow_from(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNOPTICON_SECURITY__ALLOW_FROM", '["192.168.1.50"]')
    settings = _settings(tmp_path)
    errors, conflict = configio.guarded_write(
        settings,
        {"security": {"allow_private_networks": False}},
        peer="127.0.0.1",
        forwarded_for="",
        bind_host=None,
        allow_lockout=False,
    )
    assert errors is None
    assert conflict is not None
    assert conflict["shadowed"] is True
    assert conflict["env_shadowed"] == ["security.allow_from"]


def test_guard0_is_not_suppressed_by_allow_lockout(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNOPTICON_SECURITY__ALLOW_FROM", '["192.168.1.50"]')
    settings = _settings(tmp_path)
    errors, conflict = configio.guarded_write(
        settings,
        {"security": {"allow_private_networks": False}},
        peer="127.0.0.1",
        forwarded_for="",
        bind_host=None,
        allow_lockout=True,  # "I accept the lockout risk" -- not this
    )
    assert errors is None
    assert conflict is not None
    assert conflict["shadowed"] is True


def test_guard0_does_not_fire_when_partial_does_not_touch_security(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNOPTICON_SECURITY__ALLOW_FROM", '["192.168.1.50"]')
    settings = _settings(tmp_path)
    errors, conflict = configio.guarded_write(
        settings,
        {"storage": {}},
        peer="127.0.0.1",
        forwarded_for="",
        bind_host=None,
        allow_lockout=False,
    )
    assert errors is None
    assert conflict is None


# -- guards 1-3 ARE overridable by allow_lockout=1 ---------------------------


def test_allow_lockout_skips_guards_1_through_3(tmp_path):
    settings = _settings(tmp_path)
    errors, conflict = configio.guarded_write(
        settings,
        {"security": {"allow_from": ["192.168.1.50"]}},
        peer="203.0.113.9",  # would self-lock under guard 3
        forwarded_for="203.0.113.1",  # would also trip guard 1
        bind_host=None,
        allow_lockout=True,
    )
    assert errors is None
    assert conflict is None


# -- the allowlist gate itself: a refused address never reaches a static asset


@pytest.fixture
def blocked_app(stub_dist, tmp_path):
    from synopticon.web.app import create_app
    from synopticon.web.jobs import JobManager

    settings = _settings(
        tmp_path, allow_from=["192.168.1.0/24"], allow_private_networks=False
    )
    jm = JobManager(
        tmp_path / "jobs",
        command_builder=lambda argv: [sys.executable, "-c", "import sys; sys.exit(0)"],
    )
    application = create_app(settings, job_manager=jm, dist_dir=stub_dist)
    yield application
    jm.shutdown()


def test_refused_address_gets_403_on_hashed_asset(blocked_app):
    # The default TestClient peer ("testclient") does not parse to an address
    # at all, so it must be constructed explicitly (clientip.client_ip
    # docstring) -- 203.0.113.9 is neither loopback, private, nor in the list.
    with TestClient(blocked_app, client=("203.0.113.9", 1234)) as c:
        r = c.get("/assets/index-stub.js")
        assert r.status_code == 403


def test_refused_address_gets_403_on_favicon(blocked_app):
    with TestClient(blocked_app, client=("203.0.113.9", 1234)) as c:
        r = c.get("/favicon.ico")
        assert r.status_code == 403


def test_allowed_address_is_not_blocked(blocked_app):
    with TestClient(blocked_app, client=("192.168.1.50", 1234)) as c:
        r = c.get("/assets/index-stub.js")
        assert r.status_code != 403


# -- POST /api/auth/change-password (route 6, same file, W2) ----------------


@pytest.fixture
def open_app(tmp_path):
    """A plain app -- no allowlist -- for the change-password route tests."""
    from synopticon.web.app import create_app
    from synopticon.web.jobs import JobManager

    settings = _settings(tmp_path)
    jm = JobManager(
        tmp_path / "jobs",
        command_builder=lambda argv: [sys.executable, "-c", "import sys; sys.exit(0)"],
    )
    application = create_app(settings, job_manager=jm)
    yield application, settings
    jm.shutdown()


def _seed_and_login(client, settings, username="admin", password="password123"):
    from synopticon.db import store

    c = store.connect(settings.storage.db_path)
    try:
        from synopticon.web import auth as auth_pkg

        auth_pkg.create_user(c, username, password)
    finally:
        c.close()
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200


def test_change_password_revokes_other_sessions_but_keeps_this_cookie(open_app):
    app, settings = open_app
    with TestClient(app, follow_redirects=False) as other:
        _seed_and_login(other, settings)
        other_cookie = other.cookies.get("synopticon_session")
        assert other_cookie

        with TestClient(app, follow_redirects=False) as c:
            r = c.post(
                "/api/auth/login", json={"username": "admin", "password": "password123"}
            )
            assert r.status_code == 200
            r = c.post(
                "/api/auth/change-password",
                json={"current_password": "password123", "new_password": "newpass456"},
            )
            assert r.status_code == 200
            body = r.json()
            assert body["ok"] is True
            # exactly the OTHER session got revoked -- this cookie stays valid
            assert body["signed_out_others"] == 1

        # this client's own session (used to change the password) still works
        assert c.get("/api/stats").status_code == 200
        # the other, pre-existing session was revoked
        other.cookies.set("synopticon_session", other_cookie)
        assert other.get("/api/stats").status_code == 401


def test_change_password_wrong_password_403_and_logged(open_app):
    from synopticon.db import store

    app, settings = open_app
    with TestClient(app, follow_redirects=False) as c:
        _seed_and_login(c, settings)
        r = c.post(
            "/api/auth/change-password",
            json={"current_password": "wrong", "new_password": "whatever123"},
        )
        assert r.status_code == 403

    conn = store.connect(settings.storage.db_path)
    try:
        row = conn.execute(
            "SELECT event, outcome, reason FROM web_auth_log "
            "WHERE event = 'password_change' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row is not None
        assert row["outcome"] == "failure"
    finally:
        conn.close()
