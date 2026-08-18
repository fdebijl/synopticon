"""PostgreSQL backend integration tests.

Skipped unless ``SYNOPTICON_TEST_POSTGRES_DSN`` names a reachable server, so the
default suite stays offline and fast. The whole point is to exercise what a
translation unit test cannot: that the schema actually applies, that the upsert
and RETURNING paths behave like SQLite's, and that blobs and floats survive a
round trip through a different type system.

Point it at a throwaway database — every test drops and recreates the schema::

    SYNOPTICON_TEST_POSTGRES_DSN=postgresql://user@127.0.0.1:5432/synopticon_test \\
        uv run pytest tests/unit/test_postgres_backend.py -q
"""

from __future__ import annotations

import importlib.util
import json
import os

import numpy as np
import pytest

from synopticon.config import DatabaseConfig, Settings
from synopticon.db import errors, store
from synopticon.db import copy as db_copy

DSN = os.environ.get("SYNOPTICON_TEST_POSTGRES_DSN", "")

pytestmark = [
    pytest.mark.skipif(not DSN, reason="SYNOPTICON_TEST_POSTGRES_DSN not set"),
    pytest.mark.skipif(
        importlib.util.find_spec("psycopg") is None,
        reason="psycopg not installed (needs the [postgres] extra)",
    ),
]


def _settings(tmp_path) -> Settings:
    from pydantic import SecretStr

    return Settings(
        storage={"data_dir": tmp_path},
        nas={"url": "http://nas", "spaces": ["personal"]},
        database=DatabaseConfig(backend="postgres", url=SecretStr(DSN)),
    )


@pytest.fixture
def pg(tmp_path):
    """A migrated, empty PostgreSQL database."""
    settings = _settings(tmp_path)
    # A fresh public schema is the cheapest true reset, and it re-proves that
    # the migrations apply from nothing on every run.
    bootstrap = store.connect(settings)
    bootstrap.execute("DROP SCHEMA public CASCADE")
    bootstrap.execute("CREATE SCHEMA public")
    bootstrap.commit()
    bootstrap.close()
    store._pg_migrated.clear()

    conn = store.connect(settings)
    yield conn, settings
    conn.close()


def _photo(conn, pid: int, space: str = "personal", **kw) -> None:
    conn.execute(
        "INSERT INTO photos (id, space, filename, width, height, type, synced_at, deleted) "
        "VALUES (?,?,?,?,?,?,?,0)",
        (pid, space, kw.get("filename", f"p{pid}.jpg"), 4000, 3000, "photo", store.now()),
    )


def test_every_migration_applies_to_an_empty_database(pg):
    conn, _ = pg
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
        ).fetchall()
    }
    assert {"photos", "faces", "embeddings", "review_queue", "web_users", "schedules"} <= tables
    assert conn.execute("SELECT version FROM synopticon_schema_version").fetchone()[0] == len(
        store._MIGRATIONS
    )


def test_migration_is_idempotent_across_connections(pg):
    conn, settings = pg
    store._pg_migrated.clear()
    second = store.connect(settings)
    try:
        assert (
            second.execute("SELECT COUNT(*) FROM synopticon_schema_version").fetchone()[0] == 1
        )
    finally:
        second.close()


def test_upsert_uses_the_same_on_conflict_syntax_as_sqlite(pg):
    conn, _ = pg
    for name in ("first.jpg", "second.jpg"):
        conn.execute(
            "INSERT INTO photos (id, space, filename, synced_at, deleted) VALUES (?,?,?,?,0) "
            "ON CONFLICT(space, id) DO UPDATE SET "
            "filename = excluded.filename, synced_at = excluded.synced_at, deleted = 0",
            (1, "personal", name, store.now()),
        )
    conn.commit()
    rows = conn.execute("SELECT filename FROM photos").fetchall()
    assert [r["filename"] for r in rows] == ["second.jpg"]


def test_lastrowid_comes_back_via_returning(pg):
    conn, _ = pg
    _photo(conn, 1)
    ids = [
        conn.execute(
            "INSERT INTO faces (space, photo_id, detector, x, y, w, h, pipeline_version, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("personal", 1, "scrfd", float(i), 2.0, 3.0, 4.0, "pv1", store.now()),
        ).lastrowid
        for i in range(3)
    ]
    conn.commit()
    assert ids == [1, 2, 3]
    assert all(isinstance(i, int) for i in ids)


def test_blob_columns_round_trip_as_float32_vectors(pg):
    conn, _ = pg
    _photo(conn, 1)
    vec = np.arange(512, dtype=np.float32) / 7.0
    face_id = conn.execute(
        "INSERT INTO faces (space, photo_id, detector, x, y, w, h, landmarks, "
        "pipeline_version, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("personal", 1, "scrfd", 1.0, 2.0, 3.0, 4.0,
         store.vec_to_blob(np.arange(10, dtype=np.float32)), "pv1", store.now()),
    ).lastrowid
    conn.execute(
        "INSERT INTO embeddings (face_id, model, variant, dim, vec, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (face_id, "arcface_r100", "orig", 512, store.vec_to_blob(vec), store.now()),
    )
    conn.commit()
    got = store.blob_to_vec(conn.execute("SELECT vec FROM embeddings").fetchone()["vec"])
    assert np.array_equal(got, vec)


def test_bbox_floats_keep_full_double_precision(pg):
    # REAL would be float4 here and silently round these; the UNIQUE key over
    # (space, photo_id, detector, x, y, w, h) depends on them staying distinct.
    conn, _ = pg
    _photo(conn, 1)
    x = 400.12345678901234
    conn.execute(
        "INSERT INTO faces (space, photo_id, detector, x, y, w, h, pipeline_version, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        ("personal", 1, "scrfd", x, 2.0, 3.0, 4.0, "pv1", store.now()),
    )
    conn.commit()
    assert conn.execute("SELECT x FROM faces").fetchone()["x"] == x


def test_rows_support_every_access_pattern(pg):
    conn, _ = pg
    _photo(conn, 7)
    conn.commit()
    row = conn.execute("SELECT id, filename FROM photos").fetchone()
    assert row["id"] == 7 and row[0] == 7
    assert tuple(row) == (7, "p7.jpg")  # iteration yields values, not keys
    assert dict(row) == {"id": 7, "filename": "p7.jpg"}
    assert "filename" in row.keys() and len(row) == 2


def test_a_caught_error_leaves_the_connection_usable(pg):
    # PostgreSQL aborts the whole transaction on a failed statement, unlike
    # SQLite. Every recovery path in the codebase must roll back.
    conn, _ = pg
    with pytest.raises(errors.DatabaseError):
        conn.execute("SELECT COUNT(*) FROM does_not_exist")
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 0


def test_unique_violation_maps_to_integrity_error(pg):
    conn, _ = pg
    _photo(conn, 1)
    conn.commit()
    with pytest.raises(errors.IntegrityError):
        _photo(conn, 1)
    conn.rollback()


def test_review_and_stats_layers_read_identically(pg):
    from synopticon.review import lookups, queries
    from synopticon.web import stats

    conn, settings = pg
    _photo(conn, 1)
    conn.execute(
        "INSERT INTO review_queue (kind, payload_json, confidence, status, created_at) "
        "VALUES (?,?,?,?,?)",
        ("assign", json.dumps({"space": "personal", "person_id": 3}), 0.9, "pending", store.now()),
    )
    conn.commit()

    assert queries.queue_counts(conn) == {"pending": {"assign": 1}}
    assert queries.count_review_items(conn, status="pending") == 1
    assert len(queries.load_review_items(conn, settings, status="pending", limit=10)) == 1
    assert lookups.fingerprint(conn)[0] == 0  # zero faces, and no exception
    assert stats.gather_stats(conn, settings)["photos"]["personal"]["total"] == 1


def test_db_migrate_copies_a_sqlite_library_across(pg, tmp_path):
    conn, settings = pg
    src = store.connect(tmp_path / "source.db")
    now = store.now()
    _photo(src, 1)
    face_id = src.execute(
        "INSERT INTO faces (space, photo_id, detector, x, y, w, h, landmarks, "
        "pipeline_version, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("personal", 1, "merged", 400.5, 300.25, 200.125, 200.0625,
         store.vec_to_blob(np.arange(10, dtype=np.float32)), "pv1", now),
    ).lastrowid
    src.execute(
        "INSERT INTO embeddings (face_id, model, variant, dim, vec, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (face_id, "arcface_r100", "orig", 4,
         store.vec_to_blob(np.array([1, 2, 3, 4], dtype=np.float32)), now),
    )
    store.set_state(src, "auth", {"sid": "abc"})
    src.commit()

    assert db_copy.non_empty_tables(conn) == {}
    copied = db_copy.copy_database(src, conn)
    assert copied["photos"] == 1 and copied["faces"] == 1 and copied["embeddings"] == 1

    row = conn.execute("SELECT x, w, landmarks FROM faces").fetchone()
    assert row["x"] == 400.5 and row["w"] == 200.125
    assert np.array_equal(store.blob_to_vec(row["landmarks"]), np.arange(10, dtype=np.float32))
    assert store.get_state(conn, "auth") == {"sid": "abc"}

    # The identity sequence must continue past the copied ids rather than
    # restart at 1 and collide with them.
    next_id = conn.execute(
        "INSERT INTO faces (space, photo_id, detector, x, y, w, h, pipeline_version, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        ("personal", 1, "scrfd", 9.0, 9.0, 9.0, 9.0, "pv1", now),
    ).lastrowid
    conn.commit()
    assert next_id > face_id
    src.close()


def test_no_column_is_left_at_int4(pg):
    # SQLite's INTEGER is 64-bit, so every column the schema calls INTEGER has to
    # land as bigint here; int4 would silently cap a column at 2.1e9.
    narrow = conn_columns(pg[0], "integer")
    assert narrow == []


def test_millisecond_timestamps_survive_a_copy(pg, tmp_path):
    conn, _ = pg
    src = store.connect(tmp_path / "source.db")
    ms = 1_755_000_000_000  # epoch milliseconds — three orders of magnitude past int4
    src.execute(
        "INSERT INTO photos (id, space, filesize, time, indexed_time, synced_at, deleted) "
        "VALUES (?,?,?,?,?,?,0)",
        (1, "personal", 8 * 1024**3, ms // 1000, ms, store.now()),
    )
    src.commit()
    try:
        db_copy.copy_database(src, conn)
        row = conn.execute("SELECT filesize, indexed_time FROM photos").fetchone()
        assert row["indexed_time"] == ms and row["filesize"] == 8 * 1024**3
    finally:
        src.close()


def conn_columns(conn, data_type: str) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND data_type = ? AND table_name <> ?",
        (data_type, store._VERSION_TABLE),
    ).fetchall()
    return sorted((r[0], r[1]) for r in rows)


def test_web_api_serves_from_postgres(pg, tmp_path):
    from fastapi.testclient import TestClient

    from synopticon.web import auth
    from synopticon.web.app import create_app

    conn, settings = pg
    auth.create_user(conn, "admin", "hunter2hunter2")
    conn.commit()

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>spa</html>")

    with TestClient(create_app(settings, dist_dir=dist)) as client:
        assert client.get("/api/health").status_code == 200
        assert client.post(
            "/api/auth/login", json={"username": "admin", "password": "hunter2hunter2"}
        ).status_code == 200
        for path in ("/api/stats", "/api/review/counts", "/api/audit", "/api/about"):
            assert client.get(path).status_code == 200, path
        assert client.get("/api/about").json()["database"]["backend"] == "postgres"
