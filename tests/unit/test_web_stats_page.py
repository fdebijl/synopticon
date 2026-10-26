"""Tests for the Stats page API (``web/stats_page.py`` + ``web/stats_routes.py``).

Hermetic on SQLite. Every test also runs against PostgreSQL when
``SYNOPTICON_TEST_POSTGRES_DSN`` points at a throwaway server (the schema is
dropped and recreated per test, as in ``test_postgres_backend.py``).
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

import pytest

from synopticon.config import DatabaseConfig, Settings, load_settings
from synopticon.db import store

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from synopticon.web import auth, stats_page  # noqa: E402
from synopticon.web.app import create_app  # noqa: E402
from synopticon.web.jobs import JobManager  # noqa: E402

_PG_DSN = os.environ.get("SYNOPTICON_TEST_POSTGRES_DSN", "")
_UNLISTED_NAMES = ("Hidden Harry", "Deleted Dora", "Ghost Gus", "Nameless")


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture(
    params=[
        "sqlite",
        pytest.param(
            "postgres",
            marks=pytest.mark.skipif(
                not _PG_DSN or importlib.util.find_spec("psycopg") is None,
                reason="SYNOPTICON_TEST_POSTGRES_DSN not set or psycopg missing",
            ),
        ),
    ]
)
def settings(request, tmp_path):
    nas = {"url": "https://nas.test", "account": "svc", "password": "pw",
           "spaces": ["personal", "shared"]}
    if request.param == "sqlite":
        return load_settings(storage={"data_dir": tmp_path}, nas=nas)

    from pydantic import SecretStr

    pg = Settings(
        storage={"data_dir": tmp_path},
        nas=nas,
        database=DatabaseConfig(backend="postgres", url=SecretStr(_PG_DSN)),
    )
    bootstrap = store.connect(pg)
    bootstrap.execute("DROP SCHEMA public CASCADE")
    bootstrap.execute("CREATE SCHEMA public")
    bootstrap.commit()
    bootstrap.close()
    store._pg_migrated.clear()
    return pg


@pytest.fixture
def db(settings):
    c = store.connect(settings)
    yield c
    c.close()


def _trivial_builder(argv):
    return [sys.executable, "-c", "import sys; sys.exit(0)"]


@pytest.fixture
def app(settings, db, tmp_path, stub_dist):
    jm = JobManager(tmp_path / "jobs", command_builder=_trivial_builder)
    application = create_app(settings, job_manager=jm, dist_dir=stub_dist)
    yield application
    jm.shutdown()


@pytest.fixture
def client(app, db):
    auth.create_user(db, "admin", "password123")
    db.commit()
    with TestClient(app, follow_redirects=False) as c:
        c.post("/api/auth/login", json={"username": "admin", "password": "password123"})
        yield c


# --------------------------------------------------------------------------- #
# Seed
# --------------------------------------------------------------------------- #
def _utc(*args) -> int:
    return int(datetime(*args, tzinfo=timezone.utc).timestamp())


def _local(day: date, hour: int) -> int:
    return int(datetime(day.year, day.month, day.day, hour).timestamp())


def _photo(c, space, pid, *, kind="photo", taken=None, size=0, indexed=None,
           sha=None, deleted=0):
    c.execute(
        "INSERT INTO photos (id, space, filename, filesize, time, indexed_time, type, "
        "synced_at, deleted, sha256) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (pid, space, f"{pid}.jpg", size, taken,
         None if indexed is None else indexed * 1000, kind, 1, deleted, sha),
    )


def _person(c, space, pid, name, *, show=1, deleted=0):
    c.execute(
        "INSERT INTO persons (id, space, name, show, synced_at, deleted) VALUES (?,?,?,?,?,?)",
        (pid, space, name, show, 1, deleted),
    )


_syno_face_id = iter(range(1, 10_000))


def _syno_face(c, space, photo_id, person_id, name=None):
    c.execute(
        "INSERT INTO syno_faces (space, syno_face_id, photo_id, person_id, name, "
        "x1, y1, x2, y2, synced_at) VALUES (?,?,?,?,?,0,0,1,1,1)",
        (space, next(_syno_face_id), photo_id, person_id, name),
    )


def _face(c, space, photo_id, detector, w, h, score, restored=0) -> int:
    cur = c.execute(
        "INSERT INTO faces (space, photo_id, detector, x, y, w, h, det_score, restored, "
        "pipeline_version, created_at) VALUES (?,?,?,0,0,?,?,?,?,'v1',1)",
        (space, photo_id, detector, w, h, score, restored),
    )
    return int(cur.lastrowid)


def _audit(c, action, success, ts=1000, params=None):
    c.execute(
        "INSERT INTO audit_log (ts, action, params_json, success) VALUES (?,?,?,?)",
        (ts, action, None if params is None else json.dumps(params), success),
    )


def _run(c, command, origin, started, duration, status, stats):
    if stats is not None and not isinstance(stats, str):
        stats = json.dumps(stats)
    c.execute(
        "INSERT INTO command_runs (command, origin, started_at, finished_at, status, "
        "stats_json) VALUES (?,?,?,?,?,?)",
        (command, origin, started,
         None if duration is None else started + duration, status, stats),
    )


def _seed_library(c):
    _photo(c, "personal", 1, taken=_utc(2020, 3, 2, 14), size=100,
           indexed=_utc(2021, 1, 15), sha="aaa")
    _photo(c, "personal", 2, taken=_utc(2020, 3, 2, 9, 30), size=200,
           indexed=_utc(2021, 3, 10), sha="aaa")
    _photo(c, "personal", 3, kind="video", taken=_utc(2022, 7, 10, 20), size=300,
           indexed=_utc(2021, 3, 11), sha="bbb")
    _photo(c, "shared", 10, kind="live", taken=_utc(2018, 1, 1, 0), size=400,
           indexed=_utc(2021, 3, 12), sha="aaa")
    _photo(c, "shared", 11, kind=None, taken=_utc(2023, 5, 5, 12), size=50,
           indexed=_utc(2021, 3, 12))
    _photo(c, "personal", 4, taken=_utc(1990, 6, 1, 8), size=999,
           indexed=_utc(2019, 1, 1), sha="aaa", deleted=1)

    _person(c, "personal", 1, "Alice")
    _person(c, "personal", 2, "  Bob ")
    _person(c, "personal", 3, "")
    _person(c, "personal", 4, "Hidden Harry", show=0)
    _person(c, "personal", 5, "Deleted Dora", deleted=1)
    _person(c, "personal", 6, "Nameless", show=None)
    _person(c, "shared", 7, "Carol")

    for photo in (1, 2, 3, 1):
        _syno_face(c, "personal", photo, 1, "Alice")
    _syno_face(c, "personal", 2, 2, "Bob")
    _syno_face(c, "personal", 3, 3)
    _syno_face(c, "personal", 1, 4, "Hidden Harry")
    _syno_face(c, "personal", 2, 4, "Hidden Harry")
    _syno_face(c, "personal", 3, 5, "Deleted Dora")
    _syno_face(c, "personal", 3, 6, "Nameless")
    _syno_face(c, "personal", 1, 9, "Ghost Gus")  # no persons row at all
    _syno_face(c, "shared", 10, 7, "Carol")
    _syno_face(c, "shared", 11, 7, "Carol")
    _syno_face(c, "personal", 4, 1, "Alice")  # deleted photo
    _syno_face(c, "personal", 4, 2, "Bob")  # deleted photo
    _syno_face(c, "personal", 2, None)  # a face Synology holds for nobody

    f1 = _face(c, "personal", 1, "scrfd", 50, 60, 0.8)
    f2 = _face(c, "personal", 1, "merged", 10, 12, 0.95, restored=1)
    _face(c, "personal", 2, "yolo", 100, 100, 0.5)
    f4 = _face(c, "personal", 4, "scrfd", 5, 5, 0.99)  # deleted photo
    _face(c, "shared", 10, "scrfd", 30, 30, None)
    for face_id, model in ((f1, "arcface"), (f1, "adaface"), (f2, "arcface"), (f4, "arcface")):
        c.execute(
            "INSERT INTO embeddings (face_id, model, dim, vec, created_at) VALUES (?,?,1,?,1)",
            (face_id, model, b"\x00\x00\x00\x00"),
        )

    for space, photo, n in (
        ("personal", 1, 2), ("personal", 2, 1), ("personal", 3, 0),
        ("shared", 10, 1), ("shared", 11, 12), ("personal", 4, 30),
    ):
        c.execute(
            "INSERT INTO extract_log (space, photo_id, pipeline_version, face_count, "
            "processed_at) VALUES (?,?,'v1',?,1)",
            (space, photo, n),
        )

    c.execute("INSERT INTO cluster_runs (params_json, created_at) VALUES ('{}', 100)")
    run = int(c.execute(
        "INSERT INTO cluster_runs (params_json, created_at) VALUES ('{}', 200)"
    ).lastrowid)
    for cid, size, mapped in ((1, 5, 1), (2, 1, None), (3, 1, None), (4, 3, 7)):
        c.execute(
            "INSERT INTO clusters (run_id, cluster_id, size, mapped_person_id) VALUES (?,?,?,?)",
            (run, cid, size, mapped),
        )
    c.commit()
    return run


def _seed_lifetime(c, today: date):
    for status, by in (
        ("approved", "review-ui"), ("approved", "review-ui"), ("applied", "review-ui"),
        ("failed", "cli"), ("rejected", "review-ui"), ("rejected", "review-ui"),
        ("hidden", "review-ui"), ("pending", None), ("pending", None), ("pending", None),
    ):
        c.execute(
            "INSERT INTO review_queue (kind, payload_json, status, decided_by, created_at) "
            "VALUES ('assign', '{}', ?, ?, 1)",
            (status, by),
        )

    for ts in (1000, 2000, 3000):
        _audit(c, "writeback.assign.add_face", 1, ts)
    _audit(c, "writeback.assign.add_face", 0)
    _audit(c, "writeback.assign.upload_face", 1)
    _audit(c, "writeback.assign.upload_face", 1)
    for success in (1, 1, 1, 0):
        _audit(c, "writeback.assign.verify", success)
    _audit(c, "writeback.reassign.separate", 1)
    _audit(c, "writeback.reassign.separate", 1)
    _audit(c, "writeback.reassign.verify", 1)
    _audit(c, "writeback.merge", 1)
    _audit(c, "quickmerger.merge", 1)
    _audit(c, "quickmerger.rename", 1, ts=900)
    _audit(c, "quickmerger.set_show", 1)
    _audit(c, "dedupe.delete", 1, params={"item_id": [1, 2, 3], "folder_id": []})
    _audit(c, "dedupe.delete", 0, params={"item_id": [4], "folder_id": []})
    for _ in range(4):
        _audit(c, "dryrun.assign", 1, ts=600)
    _audit(c, "dryrun.delete", None, ts=600)
    _audit(c, "dryrun.delete", None, ts=600)
    _audit(c, "backup.database", 1, ts=500)

    d1, d2, d3 = today - timedelta(days=5), today - timedelta(days=1), today
    _run(c, "sync", "cli", _local(d1, 10), 100, "ok", {
        "personal.items": {"seen": 5, "upserted": 5, "deleted": 0},
        "personal.hashes": {"hashed": 3, "failed": 0},
        "shared.items": {"seen": 2},
    })
    _run(c, "extract", "gui", _local(d1, 11), 500, "ok", {
        "personal": {"photos_processed": 4, "faces_found": 6, "restored": 1,
                     "detector_counts": {"scrfd": 4, "yolo": 1, "merged": 1}},
        "shared": {"photos_processed": 1, "faces_found": 1, "restored": 0,
                   "detector_counts": {"scrfd": 1}},
    })
    _run(c, "extract", "schedule", _local(d2, 9), 50, "failed", "{not json")
    _run(c, "prune-queue", "cli", _local(d2, 10), 5, "ok", {"pruned": 3, "orphans": {}})
    _run(c, "extract", "cli", _local(d3, 1), None, "running", None)
    _run(c, "regen-crops", "gui", _local(d3, 2), 20, "cancelled",
         {"personal": {"photos": 2, "crops": 5}})
    _run(c, "dedupe", "cli", _local(d3, 3), 10, "ok", {"groups": 2, "drop": 3})
    _run(c, "sync", "cli", _local(d3, 4), 30, "ok", ["not", "a", "dict"])
    _run(c, "extract", "cli", _local(d3, 5), 1, "ok", {
        "personal": {"photos_processed": "many", "faces_found": True,
                     "detector_counts": {"scrfd": "x"}},
    })
    c.commit()


# --------------------------------------------------------------------------- #
# Auth and empty database
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", ["/api/stats/lifetime", "/api/stats/library"])
def test_stats_page_needs_a_session(app, db, path):
    auth.create_user(db, "admin", "password123")
    db.commit()
    with TestClient(app, follow_redirects=False) as c:
        assert c.get(path).status_code == 401


def test_dashboard_stats_route_is_not_shadowed(client):
    data = client.get("/api/stats").json()
    assert "photos" in data and "runs" not in data


def test_lifetime_on_an_empty_database(client):
    resp = client.get("/api/stats/lifetime")
    assert resp.status_code == 200
    data = resp.json()
    assert data["tracking_since"] is None
    assert data["first_nas_write"] is None
    runs = data["runs"]
    assert runs["total"] == runs["ok"] == runs["unfinished"] == 0
    assert runs["by_origin"] == {"cli": 0, "gui": 0, "schedule": 0}
    assert runs["by_command"] == []
    assert runs["longest"] is None and runs["busiest_day"] is None
    assert runs["active_days"] == runs["streak_days"] == runs["total_seconds"] == 0
    assert data["work"] == {
        "photos_scanned": 0, "faces_found": 0, "faces_restored": 0, "detector_counts": {},
        "photos_synced": 0, "photos_hashed": 0, "crops_regenerated": 0,
        "duplicate_groups_found": 0, "queue_items_pruned": 0,
    }
    assert data["nas"]["writes_ok"] == data["nas"]["dry_runs"] == 0
    assert data["nas"]["by_action"] == []
    assert data["review"] == {
        "decided": 0, "approved": 0, "rejected": 0, "hidden": 0, "approval_rate": None,
    }


def test_library_on_an_empty_database(client):
    resp = client.get("/api/stats/library")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data["computed_at"], int)
    photos = data["photos"]
    assert photos["total"] == photos["deleted"] == photos["bytes"] == 0
    assert photos["by_space"] == {"personal": 0, "shared": 0}
    assert photos["by_type"] == {"photo": 0, "video": 0, "live": 0, "other": 0}
    assert photos["oldest"] is None and photos["newest"] is None
    assert photos["per_year"] == [] and photos["growth"] == []
    assert photos["by_hour"] == [0] * 24 and photos["by_weekday"] == [0] * 7
    assert photos["busiest_day"] is None and photos["duplicate_copies"] == 0
    faces = data["faces"]
    assert faces["synology_tagged"] == faces["detected"] == faces["photos_scanned"] == 0
    assert faces["avg_per_photo"] is None
    assert faces["most_crowded"] is None and faces["tiniest"] is None
    assert faces["sharpest_eye"] is None
    assert [b["bucket"] for b in faces["per_photo"]] == ["0", "1", "2", "3", "4", "5–9", "10+"]
    people = data["people"]
    assert people["distribution"] == [] and people["main_character"] is None
    assert people["gini"] is None
    assert people["everyone_else"] == {"people": 0, "faces": 0, "share": 0.0}
    assert people["unlisted_share"] == people["top10_share"] == 0.0
    assert data["grouping"] is None


# --------------------------------------------------------------------------- #
# Seeded library
# --------------------------------------------------------------------------- #
def test_library_photos(client, db):
    _seed_library(db)
    photos = client.get("/api/stats/library").json()["photos"]
    assert photos["total"] == 5 and photos["deleted"] == 1
    assert photos["by_space"] == {"personal": 3, "shared": 2}
    assert photos["by_type"] == {"photo": 2, "video": 1, "live": 1, "other": 1}
    assert photos["bytes"] == 1050
    assert photos["oldest"] == {"time": _utc(2018, 1, 1, 0), "inspect": "/inspect/shared/10"}
    assert photos["newest"] == {"time": _utc(2023, 5, 5, 12), "inspect": "/inspect/shared/11"}
    assert photos["per_year"] == [
        {"year": 2018, "count": 1}, {"year": 2019, "count": 0}, {"year": 2020, "count": 2},
        {"year": 2021, "count": 0}, {"year": 2022, "count": 1}, {"year": 2023, "count": 1},
    ]
    hours = [0] * 24
    for h in (14, 9, 20, 0, 12):
        hours[h] += 1
    assert photos["by_hour"] == hours
    assert photos["by_weekday"] == [3, 0, 0, 0, 1, 0, 1]  # Mon x3, Fri, Sun
    assert photos["busiest_day"] == {"date": "2020-03-02", "count": 2}
    assert photos["growth"] == [
        {"month": "2021-01", "total": 1},
        {"month": "2021-02", "total": 1},
        {"month": "2021-03", "total": 5},
    ]
    # personal holds two live 'aaa' copies; shared's 'aaa' and the deleted one don't count.
    assert photos["duplicate_copies"] == 1


def test_library_faces(client, db):
    _seed_library(db)
    faces = client.get("/api/stats/library").json()["faces"]
    assert faces["synology_tagged"] == 13
    assert faces["synology_named"] == 7
    assert faces["detected"] == 4
    assert faces["embeddings"] == 3
    assert faces["restored"] == 1
    assert faces["detector_counts"] == {"scrfd": 2, "merged": 1, "yolo": 1}
    assert {b["bucket"]: b["photos"] for b in faces["per_photo"]} == {
        "0": 1, "1": 2, "2": 1, "3": 0, "4": 0, "5–9": 0, "10+": 1,
    }
    assert faces["photos_scanned"] == 5
    assert faces["photos_without_faces"] == 1
    assert faces["avg_per_photo"] == pytest.approx(16 / 5)
    assert faces["most_crowded"] == {"faces": 12, "inspect": "/inspect/shared/11"}
    assert faces["tiniest"] == {"width": 10, "height": 12, "inspect": "/inspect/personal/1"}
    assert faces["sharpest_eye"] == {"det_score": 0.95, "inspect": "/inspect/personal/1"}


def test_library_people_and_distribution(client, db):
    _seed_library(db)
    data = client.get("/api/stats/library").json()
    people = data["people"]
    assert people["listed_named"] == 3
    assert people["hidden"] == 1
    assert people["unnamed_or_unlisted_faces"] == 6
    assert people["one_face_people"] == 1
    assert [(p["name"], p["space"], p["person_id"], p["faces"]) for p in people["distribution"]] == [
        ("Alice", "personal", 1, 4), ("Carol", "shared", 7, 2), ("Bob", "personal", 2, 1),
    ]
    assert people["distribution"][0]["share"] == pytest.approx(4 / 13)
    assert people["distribution"][0]["person_url"] == (
        "https://nas.test/?launchApp=SYNO.Foto.AppInstance#/person/personal_space/1"
    )
    assert people["everyone_else"] == {"people": 0, "faces": 0, "share": 0.0}
    assert people["unlisted_share"] == pytest.approx(6 / 13)
    assert people["top10_share"] == pytest.approx(7 / 13)
    assert people["gini"] == pytest.approx(6 / 21)
    assert people["main_character"] == {
        "name": "Alice", "faces": 4, "share": pytest.approx(4 / 13),
        "person_url": people["distribution"][0]["person_url"],
    }
    assert sum(p["share"] for p in people["distribution"]) + people["everyone_else"]["share"] \
        + people["unlisted_share"] == pytest.approx(1.0)

    text = json.dumps(data)
    for name in _UNLISTED_NAMES:
        assert name not in text


def test_distribution_beyond_the_top_fifteen(db, settings):
    _photo(db, "personal", 1, taken=_utc(2020, 1, 1))
    for pid in range(1, 19):
        _person(db, "personal", pid, f"P{pid:02d}")
        for _ in range(pid):
            _syno_face(db, "personal", 1, pid)
    _person(db, "personal", 99, "Hidden Harry", show=0)
    for _ in range(50):
        _syno_face(db, "personal", 1, 99)
    db.commit()

    people = stats_page.library(db, settings)["people"]
    tagged = sum(range(1, 19)) + 50
    assert len(people["distribution"]) == 15
    assert people["distribution"][0]["name"] == "P18"
    assert people["everyone_else"] == {
        "people": 3, "faces": 1 + 2 + 3, "share": pytest.approx(6 / tagged),
    }
    assert people["top10_share"] == pytest.approx(sum(range(9, 19)) / tagged)
    assert people["unlisted_share"] == pytest.approx(50 / tagged)
    assert people["main_character"]["name"] == "P18"
    total = sum(p["share"] for p in people["distribution"])
    assert total + people["everyone_else"]["share"] + people["unlisted_share"] == pytest.approx(1.0)
    assert "Hidden Harry" not in json.dumps(people)


def test_library_grouping(client, db):
    run = _seed_library(db)
    assert client.get("/api/stats/library").json()["grouping"] == {
        "run_id": run, "created_at": 200, "clusters": 4, "largest": 5,
        "singletons": 2, "mapped": 2, "unmapped": 2,
    }


# --------------------------------------------------------------------------- #
# Library cache
# --------------------------------------------------------------------------- #
def test_library_cache_serves_until_the_library_changes(client, db, monkeypatch):
    _seed_library(db)
    calls = []
    real = stats_page.library

    def counting(conn, settings):
        calls.append(1)
        return real(conn, settings)

    monkeypatch.setattr(stats_page, "library", counting)

    first = client.get("/api/stats/library").json()
    assert client.get("/api/stats/library").json() == first
    assert len(calls) == 1

    _photo(db, "personal", 50, taken=_utc(2024, 1, 1))
    db.commit()
    assert client.get("/api/stats/library").json()["photos"]["total"] == 6
    assert len(calls) == 2

    _syno_face(db, "personal", 50, 1)
    db.commit()
    data = client.get("/api/stats/library").json()
    assert data["faces"]["synology_tagged"] == 14
    assert len(calls) == 3

    # A review decision is not a library change.
    db.execute(
        "INSERT INTO review_queue (kind, payload_json, status, created_at) "
        "VALUES ('assign', '{}', 'pending', 1)"
    )
    db.commit()
    client.get("/api/stats/library")
    assert len(calls) == 3


def test_a_broken_fingerprint_never_hits(db):
    db.execute("DROP TABLE extract_log")
    db.commit()
    a, b = stats_page.library_fingerprint(db), stats_page.library_fingerprint(db)
    assert a[0] is None and a != b
    # The connection is still usable after the failed statement.
    assert db.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 0


# --------------------------------------------------------------------------- #
# Lifetime
# --------------------------------------------------------------------------- #
def test_lifetime_runs(db):
    today = date(2026, 10, 26)
    _seed_lifetime(db, today)
    runs = stats_page.lifetime(db, today=today)["runs"]
    assert runs["total"] == 9
    assert (runs["ok"], runs["failed"], runs["cancelled"], runs["unfinished"]) == (6, 1, 1, 1)
    assert runs["by_origin"] == {"cli": 6, "gui": 2, "schedule": 1}
    assert [c["command"] for c in runs["by_command"]][:2] == ["extract", "sync"]
    extract = runs["by_command"][0]
    d3 = today
    assert extract == {
        "command": "extract", "total": 4, "ok": 2, "failed": 1, "cancelled": 0,
        "unfinished": 1, "last_run": _local(d3, 5), "seconds": 551,
    }
    assert runs["total_seconds"] == 716
    assert runs["longest"] == {
        "command": "extract", "seconds": 500,
        "started_at": _local(today - timedelta(days=5), 11),
    }
    assert runs["busiest_day"] == {"date": today.isoformat(), "runs": 5}
    assert runs["active_days"] == 3
    assert runs["streak_days"] == 2
    assert stats_page.lifetime(db, today=today + timedelta(days=1))["runs"]["streak_days"] == 2
    assert stats_page.lifetime(db, today=today + timedelta(days=2))["runs"]["streak_days"] == 0


def test_lifetime_over_the_route(client, db):
    _seed_lifetime(db, date.today())
    data = client.get("/api/stats/lifetime").json()
    assert data["tracking_since"] == _local(date.today() - timedelta(days=5), 10)
    assert data["first_nas_write"] == 900

    # Malformed, non-dict and non-numeric stats_json are skipped, never fatal.
    assert data["work"] == {
        "photos_scanned": 5, "faces_found": 7, "faces_restored": 1,
        "detector_counts": {"scrfd": 5, "yolo": 1, "merged": 1},
        "photos_synced": 7, "photos_hashed": 3, "crops_regenerated": 5,
        "duplicate_groups_found": 2, "queue_items_pruned": 3,
    }

    nas = data["nas"]
    assert nas["writes_ok"] == 12
    assert nas["writes_failed"] == 2
    assert nas["dry_runs"] == 6
    assert nas["faces_tagged"] == 3
    assert nas["faces_moved"] == 2
    assert nas["merges"] == 2
    assert nas["people_named"] == 1
    assert nas["duplicates_deleted"] == 3
    actions = {a["action"]: (a["ok"], a["failed"]) for a in nas["by_action"]}
    assert actions["writeback.assign.add_face"] == (3, 1)
    assert actions["dedupe.delete"] == (1, 1)
    assert not any(a.startswith(("dryrun.", "backup.")) or a.endswith(".verify") for a in actions)
    assert nas["by_action"][0]["action"] == "writeback.assign.add_face"

    assert data["review"] == {
        "decided": 7, "approved": 4, "rejected": 2, "hidden": 1,
        "approval_rate": pytest.approx(4 / 6),
    }
