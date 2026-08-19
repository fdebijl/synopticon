"""Web GUI tests for the Pipeline / Apply / Maintenance surface (ops_routes +
pages) and the HTTP-level apply/destructive consent matrix.

Fully mocked / hermetic — no real NAS, no synopticon subprocesses. Jobs are
submitted through the real ``JobManager.submit`` (which validates params and
consent *before* spawning), and the spawned process is the harmless
``command_builder`` stub. The resolved argv is asserted from the persisted job
metadata (``GET /api/jobs/{id}``), so we prove ``--apply-merges-named`` lands
and ``-Y``/``apply-all`` never do without running anything.
"""

from __future__ import annotations

import json
import sys

import pytest

from synopticon.config import load_settings
from synopticon.db import store
from synopticon.web import auth

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from synopticon.web.app import create_app  # noqa: E402
from synopticon.web.jobs import JobManager  # noqa: E402


# --------------------------------------------------------------------------- #
# Fixtures (mirror test_web_api.py)
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
    return [sys.executable, "-c", "import sys; sys.exit(0)"]


@pytest.fixture
def app(settings, tmp_path, stub_dist):
    jm = JobManager(tmp_path / "jobs", command_builder=_trivial_builder)
    application = create_app(settings, job_manager=jm, dist_dir=stub_dist)
    yield application
    jm.shutdown()


@pytest.fixture
def client(app):
    with TestClient(app, follow_redirects=False) as c:
        yield c


def _seed_user(db, username="admin", password="password123"):
    return auth.create_user(db, username, password)


def _login(client, username="admin", password="password123"):
    return client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )


def _add_item(db, kind, payload, confidence=None, status="pending"):
    cur = db.execute(
        "INSERT INTO review_queue (kind, payload_json, confidence, status, created_at) "
        "VALUES (?,?,?,?,?)",
        (kind, json.dumps(payload), confidence, status, store.now()),
    )
    db.commit()
    return int(cur.lastrowid)


def _named_pair(a="Alice", b="Bob"):
    return {
        "space": "personal",
        "person_a": {"space": "personal", "person_id": 11, "name": a},
        "person_b": {"space": "personal", "person_id": 22, "name": b},
    }


def _submit(client, name, params=None, **kw):
    body = {"name": name, "params": params or {}}
    body.update(kw)
    return client.post("/api/jobs", json=body)


def _job_argv(client, job_id):
    r = client.get(f"/api/jobs/{job_id}")
    assert r.status_code == 200
    return r.json()["argv"]


# --------------------------------------------------------------------------- #
# Pages serve the SPA shell when authenticated (Vue Router owns the content).
# The affordances themselves now live in the Vue views, not server templates.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", ["/pipeline", "/apply", "/maintenance"])
def test_authed_pages_serve_spa_shell(app, db, path):
    _seed_user(db)
    with TestClient(app, follow_redirects=False) as c:
        _login(c)
        r = c.get(path)
        assert r.status_code == 200
        assert '<div id="app">' in r.text


# --------------------------------------------------------------------------- #
# named-merge-pairs
# --------------------------------------------------------------------------- #
def test_named_merge_pairs_lists_approved(app, db):
    _seed_user(db)
    _add_item(db, "merge_named", _named_pair("Alice", "Bob"), status="approved")
    _add_item(db, "merge_named", _named_pair("Carl", "Dana"), status="pending")  # not approved
    _add_item(db, "merge", _named_pair("X", "Y"), status="approved")  # wrong kind
    with TestClient(app, follow_redirects=False) as c:
        _login(c)
        pairs = c.get("/api/review/named-merge-pairs").json()["pairs"]
    assert len(pairs) == 1
    assert pairs[0]["label_a"] == "Alice"
    assert pairs[0]["label_b"] == "Bob"


# --------------------------------------------------------------------------- #
# maintenance counts
# --------------------------------------------------------------------------- #
def test_maintenance_counts_shape(app, db):
    _seed_user(db)
    _add_item(db, "assign", {"space": "personal"}, status="pending")
    _add_item(db, "assign", {"space": "personal"}, status="approved")
    _add_item(db, "merge_named", _named_pair(), status="approved")
    _add_item(db, "assign", {"space": "personal"}, status="failed")
    _add_item(db, "assign", {"space": "personal"}, status="applied")  # not queued
    with TestClient(app, follow_redirects=False) as c:
        _login(c)
        d = c.get("/api/maintenance/counts").json()
    assert d["pending_queue"] == 1
    assert d["approved_by_kind"]["assign"] == 1
    assert d["approved_by_kind"]["merge_named"] == 1
    # What `clear-applies` would sweep: approved + failed, never applied.
    assert d["queued_applies"] == {"approved": 2, "failed": 1}
    for key in ("photos", "faces", "embeddings", "cluster_runs"):
        assert isinstance(d[key], int)
    assert set(d["crops"].keys()) == {"files", "bytes"}
    # Nothing in this fixture names a face, so nothing is orphaned.
    assert d["orphaned_queue"] == {"by_status": {}, "prunable": 0}


def _add_face(db, face_id, photo_id, crop_path="/crops/x.jpg"):
    db.execute(
        "INSERT INTO photos (id, space, synced_at) VALUES (?,'personal',?) "
        "ON CONFLICT(space, id) DO NOTHING",
        (photo_id, store.now()),
    )
    db.execute(
        "INSERT INTO faces (face_id, space, photo_id, detector, x, y, w, h, "
        "crop_path, pipeline_version, created_at) "
        "VALUES (?,'personal',?,'scrfd',0,0,1,1,?,'v1',?)",
        (face_id, photo_id, crop_path, store.now()),
    )
    db.commit()


def test_maintenance_counts_reports_orphaned_queue(app, db):
    """A re-extract renumbers face ids; the rows left pointing at the old ones
    render with no crop, and only the count makes that visible."""
    _seed_user(db)
    _add_face(db, 10, photo_id=1)
    _add_item(db, "assign", {"face_id": 10, "space": "personal"})  # alive
    _add_item(db, "assign", {"face_id": 999, "space": "personal"})
    _add_item(db, "assign", {"face_id": 998, "space": "personal"}, status="rejected")
    _add_item(db, "assign", {"face_id": 997, "space": "personal"}, status="approved")
    with TestClient(app, follow_redirects=False) as c:
        _login(c)
        d = c.get("/api/maintenance/counts").json()
    orphans = d["orphaned_queue"]
    assert orphans["by_status"] == {"pending": 1, "rejected": 1, "approved": 1}
    # `prunable` covers only what prune-queue clears without --include-approved.
    assert orphans["prunable"] == 2


def test_orphan_scan_is_cached_but_invalidates_when_the_queue_changes(app, db, monkeypatch):
    """A wall-clock TTL would show a count the user's own prune just corrected."""
    _seed_user(db)
    _add_face(db, 10, photo_id=1)
    _add_item(db, "assign", {"face_id": 10, "space": "personal"})  # alive
    orphan = _add_item(db, "assign", {"face_id": 999, "space": "personal"})

    from synopticon.review import queries as queries_mod

    calls = []
    real = queries_mod.orphan_counts
    monkeypatch.setattr(
        queries_mod,
        "orphan_counts",
        lambda c: (calls.append(1), real(c))[1],
    )

    with TestClient(app, follow_redirects=False) as c:
        _login(c)
        first = c.get("/api/maintenance/counts").json()["orphaned_queue"]
        assert first["prunable"] == 1
        # Nothing changed: served from cache, not rescanned.
        assert c.get("/api/maintenance/counts").json()["orphaned_queue"] == first
        assert len(calls) == 1

        db.execute("DELETE FROM review_queue WHERE item_id = ?", (orphan,))
        db.commit()
        after = c.get("/api/maintenance/counts").json()["orphaned_queue"]

    assert after == {"by_status": {}, "prunable": 0}
    assert len(calls) == 2


def test_orphan_scan_cache_notices_a_status_change_in_place(app, db, monkeypatch):
    """Approving an orphan moves it between statuses without changing any extent."""
    _seed_user(db)
    item = _add_item(db, "assign", {"face_id": 999, "space": "personal"})
    with TestClient(app, follow_redirects=False) as c:
        _login(c)
        assert c.get("/api/maintenance/counts").json()["orphaned_queue"]["prunable"] == 1
        db.execute(
            "UPDATE review_queue SET status = 'approved' WHERE item_id = ?", (item,)
        )
        db.commit()
        orphans = c.get("/api/maintenance/counts").json()["orphaned_queue"]
    assert orphans["by_status"] == {"approved": 1}
    assert orphans["prunable"] == 0


def test_maintenance_counts_orphan_scan_error_degrades_to_empty(app, db, monkeypatch):
    _seed_user(db)
    from synopticon.review import queries as queries_mod

    def boom(_conn):
        raise RuntimeError("scan exploded")

    monkeypatch.setattr(queries_mod, "orphan_counts", boom)
    with TestClient(app, follow_redirects=False) as c:
        _login(c)
        r = c.get("/api/maintenance/counts")
        assert r.status_code == 200  # never 500
        assert r.json()["orphaned_queue"] == {}


def test_prune_queue_requires_confirm_and_never_schedules(app, db):
    _seed_user(db)
    from synopticon.web import schedules

    with TestClient(app, follow_redirects=False) as c:
        _login(c)
        assert _submit(c, "prune-queue").status_code == 428
        r = _submit(c, "prune-queue", {}, confirm=True)
        assert r.status_code == 202
        assert _job_argv(c, r.json()["job_id"]) == ["prune-queue", "-y"]
    # Repairing what a pipeline upgrade orphaned is a one-off, not recurring work;
    # the schedulable set is deliberately a subset of what would validate.
    assert "prune-queue" not in schedules.SCHEDULABLE


def test_maintenance_counts_crops_error_degrades_to_null(app, db, monkeypatch):
    _seed_user(db)
    from synopticon.pipeline import crops as crops_mod

    def boom(_dir):
        raise RuntimeError("no crops backend")

    monkeypatch.setattr(crops_mod, "crops_disk_usage", boom)
    with TestClient(app, follow_redirects=False) as c:
        _login(c)
        r = c.get("/api/maintenance/counts")
        assert r.status_code == 200  # never 500
        assert r.json()["crops"] == {"files": None, "bytes": None}


# --------------------------------------------------------------------------- #
# Apply consent rejection matrix (HTTP level)
# --------------------------------------------------------------------------- #
def test_apply_dry_run_needs_no_consent(app, db):
    _seed_user(db)
    with TestClient(app, follow_redirects=False) as c:
        _login(c)
        r = _submit(c, "apply", {"dry_run": True, "kinds": ["merge_named"]})
        assert r.status_code == 202
        argv = _job_argv(c, r.json()["job_id"])
        assert "--apply" not in argv


def test_apply_real_without_confirm_is_428_no_leak(app, db):
    _seed_user(db)
    with TestClient(app, follow_redirects=False) as c:
        _login(c)
        r = _submit(c, "apply", {"dry_run": False})
        assert r.status_code == 428
        assert r.json()["requirement"] == "confirm"
        assert "merge named people" not in r.text


def test_apply_merge_named_flag_without_phrase_is_428(app, db):
    _seed_user(db)
    with TestClient(app, follow_redirects=False) as c:
        _login(c)
        r = _submit(
            c,
            "apply",
            {"dry_run": False, "kinds": ["merge_named"], "apply_merges": True},
            confirm=True,
        )
        assert r.status_code == 428
        assert r.json()["detail"] == "merge_named"
        assert "merge named people" not in r.text


def test_apply_merge_named_wrong_phrase_is_428(app, db):
    _seed_user(db)
    with TestClient(app, follow_redirects=False) as c:
        _login(c)
        r = _submit(
            c,
            "apply",
            {"dry_run": False, "kinds": ["merge_named"]},
            confirm=True,
            confirm_phrase="not the phrase",
        )
        assert r.status_code == 428
        assert "merge named people" not in r.text


def test_apply_merge_named_correct_phrase_builds_gated_argv(app, db):
    _seed_user(db)
    with TestClient(app, follow_redirects=False) as c:
        _login(c)
        r = _submit(
            c,
            "apply",
            {"dry_run": False, "kinds": ["merge_named"]},
            confirm=True,
            confirm_phrase="merge named people",
        )
        assert r.status_code == 202
        argv = _job_argv(c, r.json()["job_id"])
        assert "--apply" in argv
        assert "--apply-merges-named" in argv
        # The GUI must never reach apply-all / -Y.
        assert "-Y" not in argv
        assert "apply-all" not in argv


# --------------------------------------------------------------------------- #
# Destructive-job phrase gates
# --------------------------------------------------------------------------- #
def test_dedupe_apply_phrase_gate(app, db):
    _seed_user(db)
    with TestClient(app, follow_redirects=False) as c:
        _login(c)
        # dry-run is free
        assert _submit(c, "dedupe", {"exact": True}).status_code == 202
        # apply without the phrase -> 428
        r = _submit(c, "dedupe", {"exact": True, "apply": True})
        assert r.status_code == 428
        assert "delete duplicates" not in r.text
        # apply with the phrase -> 202, gated argv
        r2 = _submit(
            c,
            "dedupe",
            {"exact": True, "apply": True},
            confirm_phrase="delete duplicates",
        )
        assert r2.status_code == 202
        argv = _job_argv(c, r2.json()["job_id"])
        assert "--apply" in argv and "-y" in argv


def test_reset_all_phrase_gate(app, db):
    _seed_user(db)
    with TestClient(app, follow_redirects=False) as c:
        _login(c)
        r = _submit(c, "reset", {"all": True}, confirm=True)  # confirm != phrase
        assert r.status_code == 428
        assert "reset all" not in r.text
        r2 = _submit(c, "reset", {"all": True}, confirm_phrase="reset all")
        assert r2.status_code == 202
        argv = _job_argv(c, r2.json()["job_id"])
        assert "--all" in argv and "-y" in argv
        assert "-Y" not in argv
