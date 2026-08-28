"""web.auth: users, sessions, API keys, login rate limiter, migration idempotence."""

from __future__ import annotations

import sqlite3

import pytest

from synopticon.db import store
from synopticon.web import auth


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "synopticon.db")
    yield c
    c.close()


# -- migration ---------------------------------------------------------------


def test_migration_creates_web_tables(conn):
    names = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {
        "web_users",
        "web_sessions",
        "web_api_keys",
        "web_totp",
        "web_recovery_codes",
        "web_login_challenges",
        "web_auth_log",
    } <= names
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 10


def test_migration_idempotent(tmp_path):
    path = tmp_path / "synopticon.db"
    c1 = store.connect(path)
    v1 = c1.execute("PRAGMA user_version").fetchone()[0]
    auth.create_user(c1, "alice", "pw")
    c1.close()
    # Reconnecting re-runs _migrate; it must not re-apply or wipe data.
    c2 = store.connect(path)
    assert c2.execute("PRAGMA user_version").fetchone()[0] == v1
    assert auth.has_users(c2) is True
    c2.close()


# -- users -------------------------------------------------------------------


def test_create_and_verify_user(conn):
    uid = auth.create_user(conn, "alice", "correct horse")
    assert auth.verify_password(conn, "alice", "correct horse") == uid
    assert auth.verify_password(conn, "alice", "wrong") is None
    assert auth.verify_password(conn, "nobody", "correct horse") is None


def test_password_not_stored_plaintext(conn):
    auth.create_user(conn, "alice", "s3cret")
    row = conn.execute("SELECT password_scrypt, salt FROM web_users").fetchone()
    assert b"s3cret" not in bytes(row["password_scrypt"])
    assert len(bytes(row["salt"])) == 16


def test_duplicate_username_rejected(conn):
    auth.create_user(conn, "alice", "pw")
    with pytest.raises(auth.UsernameTakenError):
        auth.create_user(conn, "alice", "other")


def test_has_users(conn):
    assert auth.has_users(conn) is False
    auth.create_user(conn, "alice", "pw")
    assert auth.has_users(conn) is True


def test_change_password(conn):
    uid = auth.create_user(conn, "alice", "old")
    auth.change_password(conn, uid, "new")
    assert auth.verify_password(conn, "alice", "old") is None
    assert auth.verify_password(conn, "alice", "new") == uid


# -- sessions ----------------------------------------------------------------


def test_session_roundtrip_and_logout(conn):
    uid = auth.create_user(conn, "alice", "pw")
    token = auth.create_session(conn, uid)
    assert auth.validate_session(conn, token) == uid
    # token itself is never stored, only its hash
    assert conn.execute(
        "SELECT token_hash FROM web_sessions"
    ).fetchone()["token_hash"] != token
    auth.delete_session(conn, token)
    assert auth.validate_session(conn, token) is None


def test_session_unknown_and_empty_token(conn):
    assert auth.validate_session(conn, "bogus") is None
    assert auth.validate_session(conn, "") is None


def test_session_expiry(conn):
    uid = auth.create_user(conn, "alice", "pw")
    token = auth.create_session(conn, uid, ttl_days=30)
    # force expiry into the past
    conn.execute("UPDATE web_sessions SET expires_at = 1")
    conn.commit()
    assert auth.validate_session(conn, token) is None
    # expired session is cleaned up
    assert conn.execute("SELECT COUNT(*) FROM web_sessions").fetchone()[0] == 0


def test_purge_expired(conn):
    uid = auth.create_user(conn, "alice", "pw")
    live = auth.create_session(conn, uid)
    auth.create_session(conn, uid)
    conn.execute(
        "UPDATE web_sessions SET expires_at = 1 WHERE token_hash != ?",
        (auth._sha256_hex(live),),
    )
    conn.commit()
    assert auth.purge_expired(conn) == 1
    assert auth.validate_session(conn, live) == uid


def test_session_last_seen_bump_throttled(conn):
    uid = auth.create_user(conn, "alice", "pw")
    token = auth.create_session(conn, uid)
    th = auth._sha256_hex(token)
    # simulate a recent last_seen: a second validate should not rewrite it
    conn.execute("UPDATE web_sessions SET last_seen_at = last_seen_at - 5 WHERE token_hash = ?", (th,))
    conn.commit()
    before = conn.execute(
        "SELECT last_seen_at FROM web_sessions WHERE token_hash = ?", (th,)
    ).fetchone()["last_seen_at"]
    auth.validate_session(conn, token)
    after = conn.execute(
        "SELECT last_seen_at FROM web_sessions WHERE token_hash = ?", (th,)
    ).fetchone()["last_seen_at"]
    assert after == before  # within the 60s window, not bumped

    # push last_seen well into the past -> next validate bumps it
    conn.execute("UPDATE web_sessions SET last_seen_at = last_seen_at - 120 WHERE token_hash = ?", (th,))
    conn.commit()
    auth.validate_session(conn, token)
    bumped = conn.execute(
        "SELECT last_seen_at FROM web_sessions WHERE token_hash = ?", (th,)
    ).fetchone()["last_seen_at"]
    assert bumped > before


def test_session_cascade_delete_with_user(conn):
    uid = auth.create_user(conn, "alice", "pw")
    auth.create_session(conn, uid)
    conn.execute("DELETE FROM web_users WHERE id = ?", (uid,))
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM web_sessions").fetchone()[0] == 0


# -- API keys ----------------------------------------------------------------


def test_api_key_create_validate_revoke(conn):
    key = auth.create_api_key(conn, "extension")
    assert key.startswith("syn_")
    assert len(key) == len("syn_") + 32
    kid = auth.validate_api_key(conn, key)
    assert kid is not None
    auth.revoke_api_key(conn, kid)
    assert auth.validate_api_key(conn, key) is None


def test_api_key_not_stored_plaintext(conn):
    key = auth.create_api_key(conn, "extension")
    row = conn.execute("SELECT key_hash, key_prefix FROM web_api_keys").fetchone()
    assert row["key_hash"] != key
    assert key.startswith(row["key_prefix"])


def test_api_key_validate_unknown_and_empty(conn):
    assert auth.validate_api_key(conn, "syn_deadbeef") is None
    assert auth.validate_api_key(conn, "") is None


def test_api_key_last_used_bumped(conn):
    key = auth.create_api_key(conn, "extension")
    assert conn.execute("SELECT last_used_at FROM web_api_keys").fetchone()["last_used_at"] is None
    auth.validate_api_key(conn, key)
    assert conn.execute("SELECT last_used_at FROM web_api_keys").fetchone()["last_used_at"] is not None


def test_list_api_keys_shows_prefix_not_hash(conn):
    key = auth.create_api_key(conn, "extension")
    auth.create_api_key(conn, "other")
    listed = auth.list_api_keys(conn)
    assert len(listed) == 2
    for entry in listed:
        assert "key_hash" not in entry
        assert set(entry) == {"id", "name", "key_prefix", "created_at", "last_used_at", "revoked"}
        assert entry["key_prefix"].startswith("syn_")
    assert key.startswith(listed[0]["key_prefix"])


def test_list_api_keys_reflects_revoked(conn):
    key = auth.create_api_key(conn, "extension")
    kid = auth.validate_api_key(conn, key)
    auth.revoke_api_key(conn, kid)
    assert auth.list_api_keys(conn)[0]["revoked"] is True


# -- login rate limiter ------------------------------------------------------


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def test_rate_limiter_backoff_doubles(conn):
    clock = FakeClock()
    rl = auth.LoginRateLimiter(base_seconds=2.0, cap_seconds=300.0, clock=clock)
    ip, user = "10.0.0.1", "alice"

    assert rl.check(ip, user) is True  # fresh
    rl.record_failure(ip, user)
    assert rl.check(ip, user) is False  # locked for 2s

    clock.advance(2.0)
    assert rl.check(ip, user) is True  # window elapsed
    rl.record_failure(ip, user)  # second failure -> 4s
    assert rl.check(ip, user) is False
    clock.advance(2.0)
    assert rl.check(ip, user) is False  # still locked (needs 4s)
    clock.advance(2.0)
    assert rl.check(ip, user) is True


def test_rate_limiter_cap(conn):
    clock = FakeClock()
    rl = auth.LoginRateLimiter(base_seconds=2.0, cap_seconds=5.0, clock=clock)
    for _ in range(10):
        rl.record_failure("ip", "u")
    # delay capped at 5s regardless of failure count
    assert rl.check("ip", "u") is False
    clock.advance(5.0)
    assert rl.check("ip", "u") is True


def test_rate_limiter_success_resets(conn):
    clock = FakeClock()
    rl = auth.LoginRateLimiter(base_seconds=2.0, clock=clock)
    rl.record_failure("ip", "u")
    assert rl.check("ip", "u") is False
    rl.record_success("ip", "u")
    assert rl.check("ip", "u") is True


def test_rate_limiter_isolated_per_pair(conn):
    clock = FakeClock()
    rl = auth.LoginRateLimiter(clock=clock)
    rl.record_failure("ip1", "alice")
    assert rl.check("ip1", "alice") is False
    assert rl.check("ip2", "alice") is True
    assert rl.check("ip1", "bob") is True


# -- JSON auth endpoints (SPA): /api/auth/{login,logout,me} ------------------
#
# These exercise the FastAPI app end-to-end (fully hermetic — no NAS, no real
# subprocesses; jobs stubbed via the command_builder seam).

fastapi = pytest.importorskip("fastapi")
import sys  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from synopticon.config import load_settings  # noqa: E402
from synopticon.web.app import create_app  # noqa: E402
from synopticon.web.jobs import JobManager  # noqa: E402


def _trivial_builder(argv):
    return [sys.executable, "-c", "import sys; sys.exit(0)"]


@pytest.fixture
def settings(tmp_path):
    return load_settings(
        storage={"data_dir": tmp_path},
        nas={"url": "https://nas.test", "account": "svc", "password": "pw"},
    )


@pytest.fixture
def db(settings):
    c = store.connect(settings.storage.db_path)
    yield c
    c.close()


@pytest.fixture
def app(settings, tmp_path):
    jm = JobManager(tmp_path / "jobs", command_builder=_trivial_builder)
    application = create_app(settings, job_manager=jm)
    yield application
    jm.shutdown()


def _seed_user(db, username="admin", password="password123"):
    return auth.create_user(db, username, password)


def test_api_login_success_sets_cookie_and_authenticates(app, db):
    _seed_user(db)
    with TestClient(app, follow_redirects=False) as c:
        r = c.post(
            "/api/auth/login", json={"username": "admin", "password": "password123"}
        )
        assert r.status_code == 200
        assert r.json() == {"ok": True, "username": "admin"}
        assert "synopticon_session" in r.cookies
        # the session cookie now authorizes a subsequent /api call
        assert c.get("/api/stats").status_code == 200


def test_api_login_wrong_password_401(app, db):
    _seed_user(db)
    with TestClient(app, follow_redirects=False) as c:
        r = c.post("/api/auth/login", json={"username": "admin", "password": "nope"})
        assert r.status_code == 401
        assert r.json()["error"]
        # no session established
        assert c.get("/api/stats").status_code == 401


def test_api_login_rate_limited_429(app, db):
    _seed_user(db)
    with TestClient(app, follow_redirects=False) as c:
        assert (
            c.post(
                "/api/auth/login", json={"username": "admin", "password": "nope"}
            ).status_code
            == 401
        )
        # same (ip, user) is now locked out -> 429 without waiting
        assert (
            c.post(
                "/api/auth/login", json={"username": "admin", "password": "nope"}
            ).status_code
            == 429
        )


def test_api_login_requires_json_content_type_415(app, db):
    _seed_user(db)
    with TestClient(app, follow_redirects=False) as c:
        # form-encoded body -> the CSRF Content-Type gate rejects it
        r = c.post(
            "/api/auth/login", data={"username": "admin", "password": "password123"}
        )
        assert r.status_code == 415
        # and no session was created
        assert c.get("/api/stats").status_code == 401


def test_api_logout_clears_session(app, db):
    _seed_user(db)
    with TestClient(app, follow_redirects=False) as c:
        c.post("/api/auth/login", json={"username": "admin", "password": "password123"})
        assert c.get("/api/stats").status_code == 200
        # logout is a mutating /api call -> must carry the JSON content type
        # (client.ts always does). A body-less call still sends {}.
        r = c.post("/api/auth/logout", json={})
        assert r.status_code == 200
        assert r.json() == {"ok": True}
        # session is gone -> API now 401
        assert c.get("/api/stats").status_code == 401


def test_api_me_unauthenticated_returns_200(app, db):
    _seed_user(db)  # users exist, but no session on this client
    with TestClient(app, follow_redirects=False) as c:
        r = c.get("/api/auth/me")
        assert r.status_code == 200
        data = r.json()
        assert data["authenticated"] is False
        assert data["first_boot"] is False
        assert data["username"] is None
        assert data["version"]


def test_api_me_authenticated_reports_username(app, db):
    _seed_user(db)
    with TestClient(app, follow_redirects=False) as c:
        c.post("/api/auth/login", json={"username": "admin", "password": "password123"})
        data = c.get("/api/auth/me").json()
        assert data["authenticated"] is True
        assert data["username"] == "admin"
        assert data["first_boot"] is False


def test_api_me_first_boot_returns_200(app):
    # No users seeded -> first boot. /api/auth/me is allowlisted in that branch.
    with TestClient(app, follow_redirects=False) as c:
        r = c.get("/api/auth/me")
        assert r.status_code == 200
        data = r.json()
        assert data["first_boot"] is True
        assert data["authenticated"] is False


def test_api_login_blocked_during_first_boot(app):
    # Login is NOT allowlisted during first boot; it 403s (setup required).
    with TestClient(app, follow_redirects=False) as c:
        r = c.post("/api/auth/login", json={"username": "x", "password": "y"})
        assert r.status_code == 403
        assert r.json().get("setup") is True


def test_list_users_exposes_no_secrets(conn):
    auth.create_user(conn, "alice", "pw")
    (row,) = auth.list_users(conn)
    assert row["username"] == "alice"
    assert set(row) == {"id", "username", "created_at"}


def test_delete_user_sessions_revokes_only_that_user(conn):
    a = auth.create_user(conn, "alice", "pw")
    b = auth.create_user(conn, "bob", "pw")
    ta, tb = auth.create_session(conn, a), auth.create_session(conn, b)
    assert auth.delete_user_sessions(conn, a) == 1
    assert auth.validate_session(conn, ta) is None
    assert auth.validate_session(conn, tb) == b
