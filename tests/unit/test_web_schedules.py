"""HTTP surface for saved schedules (``/api/schedules/*``).

Hermetic: the job manager's ``command_builder`` is the usual harmless stub, so a
"run now" spawns a process that exits immediately. What is asserted here is the
API contract — auth gating, the CSRF/JSON gate, 422s for unschedulable forms,
and that a manual run leaves the schedule's next firing alone.
"""

from __future__ import annotations

import sys
import time

import pytest

from synopticon.config import load_settings
from synopticon.db import store
from synopticon.web import auth

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from synopticon.web.app import create_app  # noqa: E402
from synopticon.web.jobs import JobManager  # noqa: E402


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
    return [sys.executable, "-c", "import sys; sys.exit(0)"]


@pytest.fixture
def app(settings, tmp_path, stub_dist):
    jm = JobManager(tmp_path / "jobs", command_builder=_trivial_builder)
    application = create_app(settings, job_manager=jm, dist_dir=stub_dist)
    yield application
    jm.shutdown()


@pytest.fixture
def client(app, db):
    auth.create_user(db, "admin", "password123")
    with TestClient(app, follow_redirects=False) as c:
        c.post("/api/auth/login", json={"username": "admin", "password": "password123"})
        yield c


def _delete(client, sid):
    # Every mutating /api call must carry Content-Type: application/json (the
    # CSRF gate) — including body-less ones, which is what the SPA's client does.
    return client.request("DELETE", f"/api/schedules/{sid}", json={})


def _create(client, **over):
    body = {"name": "Nightly sync", "job": "sync", "cron": "0 3 * * *", "params": {}}
    body.update(over)
    return client.post("/api/schedules", json=body)


# --------------------------------------------------------------------------- #
# Gating
# --------------------------------------------------------------------------- #
def test_requires_authentication(app, db):
    auth.create_user(db, "admin", "password123")
    with TestClient(app, follow_redirects=False) as c:
        assert c.get("/api/schedules").status_code == 401
        assert c.post("/api/schedules", json={}).status_code == 401


def test_mutations_require_json_content_type(client):
    r = client.post("/api/schedules", content="job=sync")
    assert r.status_code == 415


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #
def test_create_then_list(client):
    r = _create(client)
    assert r.status_code == 201
    row = r.json()
    assert row["job"] == "sync"
    assert row["next_run_at"] > time.time()

    listing = client.get("/api/schedules").json()
    assert [s["id"] for s in listing["items"]] == [row["id"]]
    assert listing["items"][0]["runs"] == []
    # The catalog rides along so the form never has to guess what exists.
    assert {j["job"] for j in listing["jobs"]}
    assert all("label" in j and "fields" in j for j in listing["jobs"])


def test_update_and_delete(client):
    sid = _create(client).json()["id"]
    r = client.put(
        f"/api/schedules/{sid}",
        json={"name": "Weekly", "job": "sync", "cron": "@weekly", "params": {}},
    )
    assert r.status_code == 200 and r.json()["cron"] == "@weekly"

    assert _delete(client, sid).status_code == 200
    assert client.get(f"/api/schedules/{sid}").status_code == 404
    assert _delete(client, sid).status_code == 404


def test_toggle_enabled(client):
    sid = _create(client).json()["id"]
    off = client.post(f"/api/schedules/{sid}/enabled", json={"enabled": False}).json()
    assert off["enabled"] is False and off["next_run_at"] is None
    on = client.post(f"/api/schedules/{sid}/enabled", json={"enabled": True}).json()
    assert on["enabled"] is True and on["next_run_at"] > time.time()


def test_unknown_schedule_is_404(client):
    assert client.get("/api/schedules/4242").status_code == 404
    assert (
        client.post("/api/schedules/4242/enabled", json={"enabled": True}).status_code
        == 404
    )
    assert client.post("/api/schedules/4242/run", json={}).status_code == 404


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "over",
    [
        {"cron": "0 99 * * *"},
        {"job": "reset"},
        {"job": "nope"},
        {"job": ""},
        {"timezone": "Mars/Olympus"},
    ],
)
def test_invalid_payloads_are_422(client, over):
    assert _create(client, **over).status_code == 422


def test_typed_phrase_forms_are_refused(client):
    r = _create(
        client,
        job="apply",
        params={"dry_run": False, "kinds": ["merge_named"]},
        confirm=True,
    )
    assert r.status_code == 422
    assert "typed confirmation" in r.json()["error"]
    # And the phrase itself is never echoed back to the client.
    assert "merge named people" not in r.text


def test_apply_write_needs_confirm(client):
    assert (
        _create(
            client, job="apply", params={"dry_run": False, "kinds": ["assign"]}
        ).status_code
        == 422
    )
    assert (
        _create(
            client,
            job="apply",
            params={"dry_run": False, "kinds": ["assign"]},
            confirm=True,
        ).status_code
        == 201
    )


def test_preview(client):
    r = client.post("/api/schedules/preview", json={"cron": "*/5 * * * *"})
    assert r.status_code == 200
    fires = r.json()["next"]
    assert len(fires) == 5 and fires == sorted(fires)
    assert client.post("/api/schedules/preview", json={"cron": "nope"}).status_code == 422


# --------------------------------------------------------------------------- #
# Run now
# --------------------------------------------------------------------------- #
def test_run_now_starts_a_job_without_moving_the_next_firing(client):
    created = _create(client).json()
    r = client.post(f"/api/schedules/{created['id']}/run", json={})
    assert r.status_code == 202
    body = r.json()
    assert body["job_id"]
    assert body["schedule"]["next_run_at"] == created["next_run_at"]
    assert body["schedule"]["last_status"] == "submitted"

    # The firing shows up in the schedule's history with the job's live state.
    detail = client.get(f"/api/schedules/{created['id']}").json()
    assert detail["runs"][0]["job_id"] == body["job_id"]
    assert detail["runs"][0]["job_state"] in (
        "queued",
        "running",
        "succeeded",
        "failed",
        "cancelled",
    )


def test_run_now_conflicts_when_the_same_job_is_in_flight(client, monkeypatch):
    sid = _create(client).json()["id"]
    from synopticon.web.scheduler import Scheduler

    monkeypatch.setattr(Scheduler, "_already_running", lambda self, name: True)
    r = client.post(f"/api/schedules/{sid}/run", json={})
    assert r.status_code == 409
    assert r.json()["detail"] == "skipped"
