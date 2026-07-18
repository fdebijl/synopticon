"""Web GUI API tests: auth flow, middleware gating, stats, review, jobs, SSE.

Fully mocked / hermetic — no real NAS, no synopticon subprocesses. Job spawning
is stubbed via the JobManager ``command_builder`` injection seam (a trivial
``python -c`` that exits immediately), and consent/queue errors are exercised
through the real ``submit`` path (which validates before spawning) or by
monkeypatching a single method.
"""

from __future__ import annotations

import json
import sys
import time

import pytest

from synopticon.config import load_settings
from synopticon.db import store
from synopticon.web import auth

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from synopticon.web.app import create_app  # noqa: E402
from synopticon.web.jobs import JobManager, QueueFullError  # noqa: E402


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
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


def _trivial_builder(argv):
    """Command builder that ignores argv and runs a fast, harmless process."""
    return [sys.executable, "-c", "import sys; sys.exit(0)"]


@pytest.fixture
def app(settings, tmp_path):
    jm = JobManager(tmp_path / "jobs", command_builder=_trivial_builder)
    application = create_app(settings, job_manager=jm)
    yield application
    jm.shutdown()


@pytest.fixture
def client(app):
    with TestClient(app, follow_redirects=False) as c:
        yield c


def _seed_user(db, username="admin", password="password123"):
    return auth.create_user(db, username, password)


def _add_item(db, kind, payload, confidence=None, status="pending"):
    cur = db.execute(
        "INSERT INTO review_queue (kind, payload_json, confidence, status, created_at) "
        "VALUES (?,?,?,?,?)",
        (kind, json.dumps(payload), confidence, status, store.now()),
    )
    db.commit()
    return int(cur.lastrowid)


def _login(client, username="admin", password="password123"):
    return client.post(
        "/login", data={"username": username, "password": password}
    )


# --------------------------------------------------------------------------- #
# First-boot / create-account
# --------------------------------------------------------------------------- #
def test_fresh_db_redirects_to_setup(client):
    r = client.get("/")
    assert r.status_code == 302
    assert r.headers["location"] == "/setup"


def test_fresh_db_api_blocked_until_setup(client):
    r = client.get("/api/stats")
    assert r.status_code == 403
    assert r.json()["setup"] is True


def test_setup_page_reachable_on_fresh_db(client):
    assert client.get("/setup").status_code == 200


def test_create_account_flow(client):
    r = client.post(
        "/api/auth/create-account",
        json={"username": "admin", "password": "password123"},
    )
    assert r.status_code == 201
    # session cookie is set -> dashboard now reachable
    assert client.get("/").status_code == 200
    # a second create-account is refused once a user exists
    r2 = client.post(
        "/api/auth/create-account",
        json={"username": "other", "password": "password123"},
    )
    assert r2.status_code == 403


def test_create_account_requires_fields(client):
    r = client.post("/api/auth/create-account", json={"username": "", "password": ""})
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# Login / logout / rate limit
# --------------------------------------------------------------------------- #
def test_login_success_and_logout(app, db):
    _seed_user(db)
    with TestClient(app, follow_redirects=False) as c:
        r = _login(c)
        assert r.status_code == 302
        assert r.headers["location"] == "/"
        # authenticated pages work
        assert c.get("/review").status_code == 200
        # logout clears the session
        assert c.post("/logout").status_code == 302
        assert c.get("/", follow_redirects=False).status_code == 302


def test_login_wrong_password(app, db):
    _seed_user(db)
    with TestClient(app, follow_redirects=False) as c:
        r = _login(c, password="wrong")
        assert r.status_code == 401


def test_login_rate_limited(app, db):
    _seed_user(db)
    with TestClient(app, follow_redirects=False) as c:
        assert _login(c, password="wrong").status_code == 401
        # same (ip, user) is now locked out -> 429 without waiting
        assert _login(c, password="wrong").status_code == 429


# --------------------------------------------------------------------------- #
# Auth middleware: page redirect vs API 401
# --------------------------------------------------------------------------- #
def test_unauthenticated_page_redirects_to_login(app, db):
    _seed_user(db)
    with TestClient(app, follow_redirects=False) as c:
        r = c.get("/review")
        assert r.status_code == 302
        assert r.headers["location"].startswith("/login")


def test_unauthenticated_api_returns_401(app, db):
    _seed_user(db)
    with TestClient(app, follow_redirects=False) as c:
        r = c.get("/api/stats")
        assert r.status_code == 401
        assert r.json()["error"]


def test_mutating_api_requires_json_content_type(app, db):
    _seed_user(db)
    with TestClient(app, follow_redirects=False) as c:
        _login(c)
        # form-encoded body to a mutating API endpoint -> 415
        r = c.post("/api/review/1/decide", data={"decision": "approve"})
        assert r.status_code == 415


# --------------------------------------------------------------------------- #
# API-key auth
# --------------------------------------------------------------------------- #
def test_api_key_auth_and_revocation(app, db):
    _seed_user(db)
    key = auth.create_api_key(db, "ci")
    with TestClient(app, follow_redirects=False) as c:
        # bearer key authenticates the API with no cookie
        r = c.get("/api/stats", headers={"Authorization": f"Bearer {key}"})
        assert r.status_code == 200
        # revoke -> 401
        row = db.execute("SELECT id FROM web_api_keys").fetchone()
        auth.revoke_api_key(db, row["id"])
        r2 = c.get("/api/stats", headers={"Authorization": f"Bearer {key}"})
        assert r2.status_code == 401


# --------------------------------------------------------------------------- #
# Stats — must degrade (not 500) when model weights are absent
# --------------------------------------------------------------------------- #
def test_stats_degrades_without_models(app, db):
    _seed_user(db)
    with TestClient(app, follow_redirects=False) as c:
        _login(c)
        r = c.get("/api/stats")
        assert r.status_code == 200
        data = r.json()
        assert data["extract"]["models_ready"] is False
        assert data["extract"]["pipeline_version"] is None
        assert data["faces"] == 0
        assert "job" in data


# --------------------------------------------------------------------------- #
# Review endpoints
# --------------------------------------------------------------------------- #
def test_review_items_pagination_and_total(app, db):
    _seed_user(db)
    ids = [_add_item(db, "assign", {"space": "personal", "photo_id": i}) for i in range(5)]
    with TestClient(app, follow_redirects=False) as c:
        _login(c)
        r = c.get("/api/review/items?kind=assign&limit=2&offset=0")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 5
        assert [it["item_id"] for it in data["items"]] == ids[:2]
        r2 = c.get("/api/review/items?kind=assign&limit=2&offset=2")
        assert [it["item_id"] for it in r2.json()["items"]] == ids[2:4]


def test_review_decide_and_counts(app, db):
    _seed_user(db)
    iid = _add_item(db, "assign", {"space": "personal"})
    with TestClient(app, follow_redirects=False) as c:
        _login(c)
        r = c.post(f"/api/review/{iid}/decide", json={"decision": "approve"})
        assert r.status_code == 200
        assert r.json()["status"] == "approved"
        counts = c.get("/api/review/counts").json()["counts"]
        assert counts["approved"]["assign"] == 1
        # bad decision -> 400
        assert c.post(f"/api/review/{iid}/decide", json={"decision": "x"}).status_code == 400


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_review_undo_reverts_to_pending(app, db, decision):
    _seed_user(db)
    iid = _add_item(db, "assign", {"space": "personal"})
    with TestClient(app, follow_redirects=False) as c:
        _login(c)
        assert (
            c.post(f"/api/review/{iid}/decide", json={"decision": decision}).json()[
                "status"
            ]
            == ("approved" if decision == "approve" else "rejected")
        )
        r = c.post(f"/api/review/{iid}/decide", json={"decision": "undo"})
        assert r.status_code == 200
        assert r.json()["status"] == "pending"
    row = db.execute(
        "SELECT status, decided_at, decided_by FROM review_queue WHERE item_id = ?",
        (iid,),
    ).fetchone()
    assert row["status"] == "pending"
    assert row["decided_at"] is None
    assert row["decided_by"] is None


@pytest.mark.parametrize("state", ["applied", "failed"])
def test_review_undo_refused_on_terminal_state(app, db, state):
    _seed_user(db)
    iid = _add_item(db, "assign", {"space": "personal"}, status=state)
    with TestClient(app, follow_redirects=False) as c:
        _login(c)
        r = c.post(f"/api/review/{iid}/decide", json={"decision": "undo"})
        assert r.status_code == 409
        assert r.json()["error"]
    # DB unchanged
    assert (
        db.execute(
            "SELECT status FROM review_queue WHERE item_id = ?", (iid,)
        ).fetchone()["status"]
        == state
    )


def test_review_undo_refused_on_never_decided(app, db):
    _seed_user(db)
    iid = _add_item(db, "assign", {"space": "personal"})  # pending
    with TestClient(app, follow_redirects=False) as c:
        _login(c)
        r = c.post(f"/api/review/{iid}/decide", json={"decision": "undo"})
        assert r.status_code == 409
    assert (
        db.execute(
            "SELECT status FROM review_queue WHERE item_id = ?", (iid,)
        ).fetchone()["status"]
        == "pending"
    )


def test_review_bulk_and_name(app, db):
    _seed_user(db)
    _add_item(db, "assign", {"space": "personal"}, confidence=0.9)
    _add_item(db, "assign", {"space": "personal"}, confidence=0.2)
    np_id = _add_item(db, "new_person", {"face_ids": [1, 2]})
    with TestClient(app, follow_redirects=False) as c:
        _login(c)
        r = c.post("/api/review/bulk", json={"kind": "assign", "min_confidence": 0.5})
        assert r.json()["approved"] == 1
        r2 = c.post(f"/api/review/{np_id}/name", json={"name": "Carol"})
        assert r2.status_code == 200
        assert r2.json()["suggested_name"] == "Carol"


# --------------------------------------------------------------------------- #
# Review page — Grid / Focus layout toggle (?view=)
# --------------------------------------------------------------------------- #
def test_review_page_defaults_to_grid(app, db):
    _seed_user(db)
    with TestClient(app, follow_redirects=False) as c:
        _login(c)
        r = c.get("/review")
        assert r.status_code == 200
        body = r.text
        assert "view-focus" not in body
        assert 'data-view="grid" aria-pressed="true"' in body
        assert 'view: "grid"' in body


def test_review_page_focus_view(app, db):
    _seed_user(db)
    with TestClient(app, follow_redirects=False) as c:
        _login(c)
        r = c.get("/review?view=focus")
        assert r.status_code == 200
        body = r.text
        assert "view-focus" in body
        assert 'id="focus-view"' in body
        assert 'data-view="focus" aria-pressed="true"' in body
        assert 'view: "focus"' in body


def test_review_page_bogus_view_sanitized_to_grid(app, db):
    _seed_user(db)
    with TestClient(app, follow_redirects=False) as c:
        _login(c)
        r = c.get("/review?view=bogus")
        assert r.status_code == 200
        body = r.text
        assert "view-focus" not in body
        assert 'data-view="grid" aria-pressed="true"' in body
        assert 'view: "grid"' in body


# --------------------------------------------------------------------------- #
# Jobs — consent mapping, param/queue errors, submit, SSE
# --------------------------------------------------------------------------- #
def test_job_missing_confirm_maps_to_428_without_leaking_phrase(app, db):
    _seed_user(db)
    with TestClient(app, follow_redirects=False) as c:
        _login(c)
        r = c.post("/api/jobs", json={"name": "apply", "params": {"dry_run": False}})
        assert r.status_code == 428
        body = r.json()
        assert body["requirement"] == "confirm"
        # the expected phrase text is never disclosed
        assert "merge named people" not in r.text


def test_job_merge_named_requires_phrase_no_leak(app, db):
    _seed_user(db)
    with TestClient(app, follow_redirects=False) as c:
        _login(c)
        r = c.post(
            "/api/jobs",
            json={
                "name": "apply",
                "params": {"dry_run": False, "kinds": ["merge_named"]},
                "confirm": True,
            },
        )
        assert r.status_code == 428
        assert r.json()["detail"] == "merge_named"
        assert "merge named people" not in r.text


def test_job_unknown_name_maps_to_422(app, db):
    _seed_user(db)
    with TestClient(app, follow_redirects=False) as c:
        _login(c)
        r = c.post("/api/jobs", json={"name": "definitely-not-a-job"})
        assert r.status_code == 422


def test_job_queue_full_maps_to_409(app, db, monkeypatch):
    _seed_user(db)
    with TestClient(app, follow_redirects=False) as c:
        _login(c)

        def boom(*a, **k):
            raise QueueFullError("full")

        monkeypatch.setattr(app.state.job_manager, "submit", boom)
        r = c.post("/api/jobs", json={"name": "sync"})
        assert r.status_code == 409


def _wait_terminal(jm, job_id, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        meta = jm.get(job_id)
        if meta and meta["state"] in (
            "succeeded",
            "failed",
            "cancelled",
            "interrupted",
        ):
            return meta
        time.sleep(0.05)
    raise AssertionError("job did not finish in time")


def test_job_submit_and_sse_stream(app, db):
    _seed_user(db)
    jm = app.state.job_manager
    with TestClient(app, follow_redirects=False) as c:
        _login(c)
        r = c.post("/api/jobs", json={"name": "cluster"})
        assert r.status_code == 202
        job_id = r.json()["job_id"]
        _wait_terminal(jm, job_id)
        # SSE endpoint advertises the event stream and closes on the terminal job
        with c.stream("GET", f"/api/jobs/{job_id}/stream") as s:
            assert s.status_code == 200
            assert s.headers["content-type"].startswith("text/event-stream")
            body = "".join(s.iter_text())
        assert "final" in body
