"""Tests for the Utilities page's two backups: settings TOML + database snapshot.

Hermetic — no NAS, no PostgreSQL server, no subprocess. The PostgreSQL branch of
``db.snapshot`` is exercised by pointing ``store.connect`` at a second SQLite
file, which is the only part of it that is not PostgreSQL's own code: the
table-ordered copy into a fresh, migrated target.
"""

from __future__ import annotations

import sqlite3
import sys
import time

import pytest

from synopticon.config import load_settings
from synopticon.db import store

pytest.importorskip("tomlkit")
pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from synopticon.db import snapshot as db_snapshot  # noqa: E402
from synopticon.web import auth, backup_routes  # noqa: E402
from synopticon.web.app import create_app  # noqa: E402
from synopticon.web.configio import export_config  # noqa: E402
from synopticon.web.jobs import JobManager  # noqa: E402


# --------------------------------------------------------------------------- #
# Fixtures (mirror test_web_ops.py)
# --------------------------------------------------------------------------- #
@pytest.fixture
def settings(tmp_path):
    return load_settings(
        storage={"data_dir": tmp_path},
        nas={"url": "https://nas.test", "account": "svc", "password": "hunter2"},
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


def _workdirs(tmp_path):
    return [p for p in tmp_path.glob(".snapshot-*")]


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "path", ["/api/backup/info", "/api/backup/config", "/api/backup/database"]
)
def test_backup_needs_a_session(app, db, path):
    auth.create_user(db, "admin", "password123")
    with TestClient(app, follow_redirects=False) as c:
        assert c.get(path).status_code == 401


# --------------------------------------------------------------------------- #
# Settings backup
# --------------------------------------------------------------------------- #
def test_info_describes_config_and_database(client, settings, tmp_path):
    data = client.get("/api/backup/info").json()
    assert data["config"]["path"] == str(tmp_path / "config.toml")
    assert data["config"]["exists"] is False
    assert "nas.password" in data["config"]["secret_keys"]
    assert data["database"]["backend"] == "sqlite"
    assert data["database"]["bytes"] > 0


def test_config_backup_blanks_secrets_by_default(settings, tmp_path):
    (tmp_path / "config.toml").write_text(
        '# my notes\n[nas]\nurl = "https://nas.test"\npassword = "hunter2"\n'
    )
    text = export_config(settings)
    assert "hunter2" not in text
    assert 'password = ""' in text
    # A verbatim copy otherwise — comments and key order survive.
    assert "# my notes" in text
    assert 'url = "https://nas.test"' in text


def test_config_backup_includes_secrets_on_request(settings, tmp_path):
    (tmp_path / "config.toml").write_text('[nas]\npassword = "hunter2"\n')
    assert 'password = "hunter2"' in export_config(settings, include_secrets=True)


def test_config_backup_without_a_file_renders_effective_settings(settings, tmp_path):
    assert not (tmp_path / "config.toml").exists()
    text = export_config(settings)
    assert 'url = "https://nas.test"' in text
    # `model_dump(mode="json")` would have written the '**********' placeholder.
    assert "*" not in text
    assert "hunter2" not in text
    assert 'password = "hunter2"' in export_config(settings, include_secrets=True)


def test_config_route_serves_a_download(client, settings, tmp_path):
    (tmp_path / "config.toml").write_text('[nas]\npassword = "hunter2"\n')
    r = client.get("/api/backup/config")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/toml")
    assert "attachment; filename=" in r.headers["content-disposition"]
    assert ".toml" in r.headers["content-disposition"]
    assert "hunter2" not in r.text

    r = client.get("/api/backup/config", params={"secrets": "1"})
    assert "hunter2" in r.text


def test_config_backup_is_audited(client, db, tmp_path):
    (tmp_path / "config.toml").write_text('[nas]\npassword = "hunter2"\n')
    client.get("/api/backup/config", params={"secrets": "1"})
    rows = db.execute(
        "SELECT action, params_json FROM audit_log ORDER BY id DESC"
    ).fetchall()
    assert rows[0]["action"] == "backup.config"
    assert '"secrets": true' in rows[0]["params_json"]
    # The audit row must not carry the credential it is recording the export of.
    assert "hunter2" not in rows[0]["params_json"]


# --------------------------------------------------------------------------- #
# Database snapshot
# --------------------------------------------------------------------------- #
def test_snapshot_is_a_usable_sqlite_copy(settings, db, tmp_path):
    db.execute(
        "INSERT INTO photos (id, space, filename, synced_at) VALUES (?,?,?,?)",
        (42, "personal", "a.jpg", 0),
    )
    db.commit()

    dest = tmp_path / "out" / "copy.db"
    db_snapshot.snapshot(settings, dest)

    raw = sqlite3.connect(dest)
    try:
        assert raw.execute("SELECT id FROM photos").fetchall() == [(42,)]
    finally:
        raw.close()


def test_snapshot_refuses_to_overwrite(settings, db, tmp_path):
    dest = tmp_path / "copy.db"
    dest.write_bytes(b"")
    with pytest.raises(FileExistsError):
        db_snapshot.snapshot(settings, dest)


def test_snapshot_without_a_database(tmp_path):
    settings = load_settings(storage={"data_dir": tmp_path / "empty"})
    with pytest.raises(FileNotFoundError):
        db_snapshot.snapshot(settings, tmp_path / "copy.db")


def test_database_route_streams_the_snapshot_and_cleans_up(client, db, tmp_path):
    db.execute(
        "INSERT INTO photos (id, space, filename, synced_at) VALUES (?,?,?,?)",
        (7, "personal", "b.jpg", 0),
    )
    db.commit()

    r = client.get("/api/backup/database")
    assert r.status_code == 200
    assert r.content.startswith(b"SQLite format 3")
    assert ".db" in r.headers["content-disposition"]

    out = tmp_path / "downloaded.db"
    out.write_bytes(r.content)
    raw = sqlite3.connect(out)
    try:
        assert raw.execute("SELECT id FROM photos").fetchall() == [(7,)]
    finally:
        raw.close()

    # The background task removed the working directory it was built in.
    assert _workdirs(tmp_path) == []


def test_database_route_reports_a_missing_database(client, monkeypatch, tmp_path):
    def boom(settings, dest):
        raise FileNotFoundError(dest)

    monkeypatch.setattr(db_snapshot, "snapshot", boom)
    r = client.get("/api/backup/database")
    assert r.status_code == 404
    assert _workdirs(tmp_path) == []


def test_database_route_reports_a_failed_snapshot(client, db, monkeypatch, tmp_path):
    def boom(settings, dest):
        raise RuntimeError("disk full")

    monkeypatch.setattr(db_snapshot, "snapshot", boom)
    r = client.get("/api/backup/database")
    assert r.status_code == 500
    assert "disk full" in r.json()["error"]
    assert _workdirs(tmp_path) == []
    row = db.execute(
        "SELECT action, success FROM audit_log ORDER BY id DESC"
    ).fetchone()
    assert row["action"] == "backup.database"
    assert not row["success"]


def test_stale_workdirs_are_swept(tmp_path):
    fresh = tmp_path / ".snapshot-fresh"
    stale = tmp_path / ".snapshot-stale"
    for d in (fresh, stale):
        d.mkdir()
        (d / "x.db").write_bytes(b"")
    old = time.time() - backup_routes._STALE_AFTER - 60
    import os

    os.utime(stale, (old, old))

    backup_routes._sweep(tmp_path)
    assert fresh.exists()
    assert not stale.exists()


def test_postgres_snapshot_copies_into_a_fresh_sqlite_file(tmp_path, monkeypatch):
    """The PostgreSQL branch, with a SQLite file standing in for the server.

    Everything the branch owns is exercised: the source is opened from
    ``Settings``, the target is a fresh (migrated, empty) SQLite database, and
    ``copy_database`` moves every table across.
    """
    source_settings = load_settings(storage={"data_dir": tmp_path / "src"})
    src = store.connect(source_settings.storage.db_path)
    src.execute(
        "INSERT INTO photos (id, space, filename, synced_at) VALUES (?,?,?,?)",
        (99, "shared", "c.jpg", 0),
    )
    src.commit()
    src.close()

    pg_settings = load_settings(
        storage={"data_dir": tmp_path / "src"}, database={"backend": "postgres"}
    )
    real_connect = store.connect

    def fake_connect(target, check_same_thread=True):
        if target is pg_settings:
            target = source_settings.storage.db_path
        return real_connect(target, check_same_thread)

    monkeypatch.setattr(store, "connect", fake_connect)

    dest = tmp_path / "pg-copy.db"
    db_snapshot.snapshot(pg_settings, dest)

    raw = sqlite3.connect(dest)
    try:
        assert raw.execute("SELECT id FROM photos").fetchall() == [(99,)]
    finally:
        raw.close()
