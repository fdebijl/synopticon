"""End-to-end tests for the login flow (security contract section 6): the
network allowlist gate, both throttle tiers armed on every request, the
two-step challenge's amplifier-free shape, session pinning's auth-cache
invalidation, and the no-leak assertions tied to those routes.

Several of these exercise feature bodies (`twofactor`, `throttle`, `sessions`)
that other work units are landing in parallel and that currently raise
`NotImplementedError` (or, for the two backup routes, have not yet grown their
`ident[0] == "user"` gate at all) -- see this unit's final report for exactly
which assertions are expected to go green only once those land.
"""

from __future__ import annotations

import inspect
import sys

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from synopticon.config import load_settings  # noqa: E402
from synopticon.db import store  # noqa: E402
from synopticon.web import auth  # noqa: E402
from synopticon.web.app import create_app  # noqa: E402
from synopticon.web.jobs import JobManager  # noqa: E402


def _trivial_builder(argv):
    return [sys.executable, "-c", "import sys; sys.exit(0)"]


def _make_app(tmp_path, **security):
    """A `create_app` wired like the shared `web_app` fixture, but with its own
    `[security]` overrides -- most of these tests need a non-default
    `max_failures_per_address` and the shared fixture bakes the default in via
    `web_settings`.
    """
    settings = load_settings(
        storage={"data_dir": tmp_path},
        nas={"url": "https://nas.test", "account": "svc", "password": "pw"},
        security=security,
    )
    jm = JobManager(tmp_path / "jobs", command_builder=_trivial_builder)
    app = create_app(settings, job_manager=jm)
    return settings, app, jm


@pytest.fixture
def app_bundle(tmp_path):
    settings, app, jm = _make_app(tmp_path)
    yield settings, app
    jm.shutdown()


@pytest.fixture
def db(app_bundle):
    settings, _ = app_bundle
    c = store.connect(settings.storage.db_path)
    yield c
    c.close()


def _seed_user(conn, username="bob", password="password123"):
    return auth.create_user(conn, username, password)


def _session_count(conn, user_id: int) -> int:
    """Raw SQL, deliberately not `sessions.count_user_sessions` -- that
    function is still a `NotImplementedError` stub while W4 is mid-flight, and
    a before/after comparison must not depend on the very function a test is
    trying to stay independent of.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM web_sessions WHERE user_id = ?", (user_id,)
    ).fetchone()
    return int(row["n"])


# -- both throttle tiers are armed on every request, unconditionally -------- #


def test_repeated_wrong_password_eventually_429_via_pair_tier(tmp_path):
    """N wrong-password attempts against an unenrolled account eventually 429
    with the address tier disabled (`max_failures_per_address=0`) -- i.e. the
    pair tier (the exponential backoff) is what fired.
    """
    settings, app, jm = _make_app(tmp_path, max_failures_per_address=0)
    try:
        conn = store.connect(settings.storage.db_path)
        try:
            _seed_user(conn, "bob", "correct horse battery staple")
        finally:
            conn.close()
        with TestClient(app, follow_redirects=False, client=("203.0.113.9", 1)) as c:
            statuses = []
            for _ in range(15):
                r = c.post(
                    "/api/auth/login", json={"username": "bob", "password": "wrong"}
                )
                statuses.append(r.status_code)
                if r.status_code == 429:
                    break
            assert 429 in statuses
            if r.status_code == 429:
                assert set(r.json()) == {"error", "retry_after", "recovery"}
                assert "Retry-After" in r.headers
    finally:
        jm.shutdown()


def test_loopback_login_throttled_like_any_address(tmp_path):
    """A flood from 127.0.0.1 with no X-Forwarded-For, against an app whose
    trusted_proxies is empty, is blocked exactly like a public address (D5) --
    there is no address for which login throttling stands down.
    """
    settings, app, jm = _make_app(tmp_path, max_failures_per_address=0)
    try:
        conn = store.connect(settings.storage.db_path)
        try:
            _seed_user(conn, "bob", "password123")
        finally:
            conn.close()
        with TestClient(app, follow_redirects=False, client=("127.0.0.1", 1)) as c:
            statuses = []
            for _ in range(15):
                r = c.post(
                    "/api/auth/login", json={"username": "bob", "password": "wrong"}
                )
                statuses.append(r.status_code)
                if r.status_code == 429:
                    break
            assert 429 in statuses
    finally:
        jm.shutdown()


def test_blocked_login_flood_costs_one_connection_and_one_row(tmp_path, monkeypatch):
    """Ten consecutive blocked logins from one address call
    `synopticon.db.store.connect` exactly once and write exactly one
    `web_auth_log` row -- the blocked_log gate bounds the write/connection cost
    of an attacker who ignores 429 to one per address prefix per minute, not
    one per request.
    """
    settings, app, jm = _make_app(tmp_path, max_failures_per_address=0)
    try:
        conn = store.connect(settings.storage.db_path)
        try:
            _seed_user(conn, "bob", "password123")
        finally:
            conn.close()
        with TestClient(app, follow_redirects=False, client=("203.0.113.20", 1)) as c:
            # Arm the pair-tier backoff (unblocked -- this attempt itself
            # opens a connection, deliberately not counted below).
            r0 = c.post(
                "/api/auth/login", json={"username": "bob", "password": "wrong"}
            )
            assert r0.status_code == 401

            calls = []
            real_connect = store.connect

            def spy(*a, **kw):
                calls.append((a, kw))
                return real_connect(*a, **kw)

            monkeypatch.setattr(store, "connect", spy)

            for _ in range(10):
                r = c.post(
                    "/api/auth/login",
                    json={"username": "bob", "password": "still-wrong"},
                    headers={"X-Forwarded-For": "9.9.9.9"},  # D2: ignored, untrusted
                )
                assert r.status_code == 429

            assert len(calls) == 1
        blocked_rows = real_connect(settings.storage.db_path).execute(
            "SELECT COUNT(*) AS n FROM web_auth_log WHERE outcome = 'blocked'"
        ).fetchone()["n"]
        assert blocked_rows == 1
    finally:
        jm.shutdown()


def test_login_verify_blocked_address_opens_no_connection(tmp_path, monkeypatch):
    """Ten `POST /api/auth/login/verify` calls with random challenge tokens
    from a blocked address call `store.connect` zero times (D9c) -- the
    address-only verdict at step 11 runs before hop A ever opens one.
    """
    settings, app, jm = _make_app(tmp_path, max_failures_per_address=1)
    try:
        conn = store.connect(settings.storage.db_path)
        try:
            _seed_user(conn, "bob", "password123")
        finally:
            conn.close()
        with TestClient(app, follow_redirects=False, client=("198.51.100.7", 1)) as c:
            # First failure trips the (threshold=1) address tier; the second
            # is itself blocked and writes the one `blocked_log`-gated row --
            # /api/auth/login and /api/auth/login/verify share the same
            # "blocked:<prefix>" key, so that write consumes the once-a-minute
            # allowance before the loop below ever runs.
            c.post("/api/auth/login", json={"username": "bob", "password": "wrong"})
            r_block = c.post(
                "/api/auth/login", json={"username": "bob", "password": "wrong"}
            )
            assert r_block.status_code == 429

            calls = []
            real_connect = store.connect

            def spy(*a, **kw):
                calls.append((a, kw))
                return real_connect(*a, **kw)

            monkeypatch.setattr(store, "connect", spy)

            for _ in range(10):
                r = c.post(
                    "/api/auth/login/verify",
                    json={"challenge": "x" * 43, "code": "000000"},
                )
                assert r.status_code == 429
            assert len(calls) == 0
    finally:
        jm.shutdown()


def test_login_verify_unblocked_address_charges_per_attempt(tmp_path, monkeypatch):
    """The same ten calls from an *unblocked* address call `store.connect` ten
    times (one hop-A peek each) and charge the address window ten times -- the
    amplifier is bounded (one connection per attempt), not absent.
    """
    settings, app, jm = _make_app(tmp_path)
    try:
        conn = store.connect(settings.storage.db_path)
        try:
            _seed_user(conn, "carol", "password123")
        finally:
            conn.close()
        with TestClient(app, follow_redirects=False, client=("198.51.100.9", 1)) as c:
            # Prime `have_users` (a one-way, first-request-only DB check) so it
            # is not counted below.
            c.get("/api/auth/me")

            calls = []
            real_connect = store.connect

            def spy(*a, **kw):
                calls.append((a, kw))
                return real_connect(*a, **kw)

            monkeypatch.setattr(store, "connect", spy)

            for _ in range(10):
                r = c.post(
                    "/api/auth/login/verify",
                    json={"challenge": "y" * 43, "code": "000000"},
                )
                assert r.status_code == 401
                assert r.json().get("restart") is True
            assert len(calls) == 10
    finally:
        jm.shutdown()


# -- session pinning: re-auth is mandatory, and the cache drop is immediate - #


def test_session_pinning_set_requires_password_and_revokes_nothing(app_bundle, db):
    """Route 16 with `{mode}` and no password is 422 and revokes nothing
    (R5) -- it must not be reachable with no credential at all, unlike the
    endpoint it replaced.
    """
    settings, app = app_bundle
    uid = _seed_user(db, "alice", "hunter2pass")
    with TestClient(app, follow_redirects=False) as c:
        c.post("/api/auth/login", json={"username": "alice", "password": "hunter2pass"})
        before = _session_count(db, uid)
        r = c.post("/api/auth/session-pinning", json={"mode": "device"})
        assert r.status_code == 422
        after = _session_count(db, uid)
        assert after == before


def test_session_pinning_change_drops_auth_cache_immediately(app_bundle, db):
    """Route 16 with a correct password drops the auth cache, so a session it
    deleted is unauthenticated on its very next request rather than up to
    `_AUTH_CACHE_TTL` (30s) later (D9b).
    """
    settings, app = app_bundle
    _seed_user(db, "alice", "hunter2pass")
    with TestClient(app, follow_redirects=False) as first, TestClient(
        app, follow_redirects=False
    ) as second:
        first.post(
            "/api/auth/login", json={"username": "alice", "password": "hunter2pass"}
        )
        # Warm the auth-cache entry for `first`'s cookie.
        assert first.get("/api/stats").status_code == 200

        second.post(
            "/api/auth/login", json={"username": "alice", "password": "hunter2pass"}
        )
        r = second.post(
            "/api/auth/session-pinning",
            json={"mode": "device", "password": "hunter2pass"},
        )
        assert r.status_code == 200

        # `first`'s session was one of the "other" sessions set_pin_mode just
        # deleted; its cached verdict must be gone on the very next request.
        assert first.get("/api/stats").status_code == 401


# -- /api/auth/me leaks no enrolment state to a non-owner -------------------- #


def test_me_anonymous_and_apikey_carry_no_enrolment_state(app_bundle, db):
    settings, app = app_bundle
    _seed_user(db, "alice", "hunter2pass")
    key = auth.create_api_key(db, "ci")
    with TestClient(app, follow_redirects=False) as c:
        anon = c.get("/api/auth/me").json()
        assert anon["totp_enabled"] is None
        assert anon["session_pinning"] is None

        keyed = c.get(
            "/api/auth/me", headers={"Authorization": f"Bearer {key}"}
        ).json()
        assert keyed["totp_enabled"] is None
        assert keyed["session_pinning"] is None


# -- an API key is refused everywhere this contract narrows it (R12, C4) ---- #


def test_apikey_forbidden_on_security_routes(app_bundle, db):
    settings, app = app_bundle
    key = auth.create_api_key(db, "ci")
    headers = {"Authorization": f"Bearer {key}"}
    with TestClient(app, follow_redirects=False) as c:
        assert c.get("/api/security/access", headers=headers).status_code == 403
        assert c.get("/api/security/log", headers=headers).status_code == 403
        assert c.get("/api/security/throttles", headers=headers).status_code == 403
        assert (
            c.post(
                "/api/security/throttles/clear", json={}, headers=headers
            ).status_code
            == 403
        )


def test_apikey_forbidden_on_backup_routes(app_bundle, db):
    """C4: a leaked `syn_` key must not become a bypass of this contract --
    the database snapshot carries the whole `web_users` table.

    NOTE for the report: this asserts the `ident[0] == "user"` gate section
    5.6 assigns to backup_routes.py (owned by W1 in this contract, not WA) --
    it is expected to go green once that lands, not before.
    """
    settings, app = app_bundle
    key = auth.create_api_key(db, "ci")
    headers = {"Authorization": f"Bearer {key}"}
    with TestClient(app, follow_redirects=False) as c:
        assert c.get("/api/backup/config", headers=headers).status_code == 403
        assert c.get("/api/backup/database", headers=headers).status_code == 403


# -- the network allowlist gate sits ahead of the SPA bundle, and stamps ---- #
# -- no-store on its own denial (F9) ---------------------------------------- #


def test_denied_assets_request_carries_no_store(tmp_path):
    settings, app, jm = _make_app(tmp_path, allow_from=["203.0.113.0/24"])
    try:
        with TestClient(app, follow_redirects=False, client=("198.51.100.1", 1)) as c:
            r = c.get("/assets/index-abc123.js")
            assert r.status_code == 403
            assert r.headers.get("cache-control") == "no-store"
            assert r.headers.get("content-type", "").startswith("text/html")
    finally:
        jm.shutdown()


def test_denied_api_request_body_and_no_store(tmp_path):
    settings, app, jm = _make_app(tmp_path, allow_from=["203.0.113.0/24"])
    try:
        with TestClient(app, follow_redirects=False, client=("198.51.100.1", 1)) as c:
            r = c.get("/api/auth/me")
            assert r.status_code == 403
            assert r.headers.get("cache-control") == "no-store"
            body = r.json()
            assert body["blocked"] is True
            assert body["client_ip"] == "198.51.100.1"
    finally:
        jm.shutdown()


# -- the scheduler needs no new wiring to run its housekeeping (F8) --------- #


def test_scheduler_construction_gained_no_settings_parameter(app_bundle):
    """F8: `Scheduler` must not gain a `settings` constructor parameter --
    `_housekeeping` reads `authlog.retention_policy()` instead, and a
    forgotten wiring parameter would silently disable it with no test to
    notice.
    """
    settings, app = app_bundle
    from synopticon.web.scheduler import Scheduler

    sig = inspect.signature(Scheduler.__init__)
    assert "settings" not in sig.parameters
    assert app.state.scheduler is not None


# -- regressions found reviewing the shipped code -------------------------- #


def _enrol(conn, user_id: int) -> None:
    """Put `user_id` through a complete two-step enrolment."""
    from synopticon.web import totp

    pending = auth.start_totp_enrolment(conn, user_id)
    code = totp.code_for(pending.secret, totp.current_step())
    assert auth.confirm_totp_enrolment(conn, user_id, code) is not None


def _probe_after_rejected_code(tmp_path, password: str, peer: str) -> int:
    """Run one full two-step sign-in with `password`, fail the code, then
    return the status of a *fresh* login POST for the same account.
    """
    settings, app, jm = _make_app(tmp_path, max_failures_per_address=0)
    try:
        conn = store.connect(settings.storage.db_path)
        try:
            uid = _seed_user(conn, "bob", "correct horse battery staple")
            _enrol(conn, uid)
        finally:
            conn.close()
        with TestClient(app, follow_redirects=False, client=(peer, 1)) as c:
            first = c.post(
                "/api/auth/login", json={"username": "bob", "password": password}
            )
            assert first.status_code == 200, first.text
            challenge = first.json()["challenge"]
            c.post(
                "/api/auth/login/verify",
                json={"challenge": challenge, "code": "000000"},
            )
            return c.post(
                "/api/auth/login", json={"username": "bob", "password": "whatever"}
            ).status_code
    finally:
        jm.shutdown()


def test_rejected_code_arms_password_backoff_whatever_the_password_was(tmp_path):
    """No password oracle behind the second factor.

    The password-scope backoff is what `/api/auth/login` reads, so arming it
    only when step one's password was wrong would make the next login's 429 a
    direct read-out of whether a guess was correct -- three requests per guess
    against exactly the accounts a second factor protects.
    """
    wrong = _probe_after_rejected_code(tmp_path / "a", "wrong", "203.0.113.21")
    right = _probe_after_rejected_code(
        tmp_path / "b", "correct horse battery staple", "203.0.113.22"
    )
    assert wrong == right == 429


def test_non_ascii_code_is_rejected_not_a_500(tmp_path):
    """Unicode digits reach `hmac.compare_digest`, which raises TypeError on a
    non-ASCII str -- a 500 that skipped both the throttle charge and the log.
    """
    settings, app, jm = _make_app(tmp_path)
    try:
        conn = store.connect(settings.storage.db_path)
        try:
            uid = _seed_user(conn, "bob", "correct horse battery staple")
            _enrol(conn, uid)
        finally:
            conn.close()
        with TestClient(app, follow_redirects=False, client=("203.0.113.31", 1)) as c:
            first = c.post(
                "/api/auth/login",
                json={"username": "bob", "password": "correct horse battery staple"},
            )
            challenge = first.json()["challenge"]
            r = c.post(
                "/api/auth/login/verify",
                json={"challenge": challenge, "code": "٣٣٣٣٣٣"},
            )
            assert r.status_code == 401
    finally:
        jm.shutdown()


@pytest.mark.parametrize("body", ["[]", '"x"', "not json"])
def test_login_routes_reject_a_non_object_body(app_bundle, db, body):
    """Both routes sit in the middleware's allowlist, so an unguarded parse was
    an unthrottled, unlogged 500 plus a traceback per anonymous request.
    """
    _, app = app_bundle
    _seed_user(db)
    with TestClient(app, follow_redirects=False) as c:
        for path in ("/api/auth/login", "/api/auth/login/verify"):
            r = c.post(
                path, content=body, headers={"Content-Type": "application/json"}
            )
            assert r.status_code == 422, (path, body, r.status_code)


def test_apikey_cannot_write_security_config_or_mint_keys(app_bundle, db):
    """An API key must never widen its own reach: `[security]` owns the
    allowlist, the proxy trust list and both throttle tiers, and minting a key
    would outlive the revocation of the key that did it.
    """
    _, app = app_bundle
    _seed_user(db)
    key = auth.create_api_key(db, "ci")
    headers = {"Authorization": f"Bearer {key}"}
    with TestClient(app, follow_redirects=False) as c:
        assert c.get("/api/stats", headers=headers).status_code == 200
        assert (
            c.put(
                "/api/config",
                json={"security": {"trusted_proxies": ["0.0.0.0/0"]}},
                headers=headers,
            ).status_code
            == 403
        )
        assert c.get("/api/auth/keys", headers=headers).status_code == 403
        assert (
            c.post("/api/auth/keys", json={"name": "x"}, headers=headers).status_code
            == 403
        )
