"""Shared fixtures: tmp settings + a SQLite builder for synthetic libraries."""

from __future__ import annotations

import os
from types import SimpleNamespace

import numpy as np
import pytest

from synopticon.config import load_settings
from synopticon.db import store

# Shared by the syno/* respx-mocked test modules (test_client, test_sync,
# test_writeback): a fake NAS base URL + settings with fast rate limits so
# token-bucket throttling doesn't slow the suite down.
NAS_BASE_URL = "https://nas.test"


@pytest.fixture(autouse=True)
def isolate_ambient_config(tmp_path, monkeypatch):
    """Keep the developer's real config out of the test settings.

    ``load_settings`` discovers ``./config.toml`` / ``./data/config.toml`` and
    ``.env`` relative to the CWD, plus ``SYNOPTICON_*`` env vars. Running from a
    checkout that has any of those makes settings-dependent tests pass or fail
    based on local files (e.g. a ``data/config.toml`` that sets a detection
    threshold). chdir to an empty dir and strip the env so every test sees the
    pristine defaults.
    """
    for key in [k for k in os.environ if k.startswith("SYNOPTICON_")]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def tmp_settings(tmp_path):
    return load_settings(storage={"data_dir": tmp_path})


@pytest.fixture
def nas_settings(tmp_path):
    return load_settings(
        storage={"data_dir": tmp_path},
        nas={
            "url": NAS_BASE_URL,
            "account": "svc",
            "password": "pw",
            "requests_per_second": 1000.0,
            "write_requests_per_second": 1000.0,
        },
    )


@pytest.fixture
def nas_conn(tmp_path):
    conn = store.connect(tmp_path / "synopticon.db")
    yield conn
    conn.close()


@pytest.fixture
def db_helpers(tmp_path):
    conn = store.connect(tmp_path / "synopticon.db")

    def insert_photo(space, pid, w=1000, h=1000):
        conn.execute(
            "INSERT INTO photos (id, space, width, height, synced_at) "
            "VALUES (?,?,?,?,?)",
            (pid, space, w, h, store.now()),
        )

    def insert_person(space, pid, name=None):
        conn.execute(
            "INSERT INTO persons (id, space, name, synced_at) VALUES (?,?,?,?)",
            (pid, space, name, store.now()),
        )

    def insert_person_photo(space, person_id, photo_id):
        conn.execute(
            "INSERT INTO person_photos (space, person_id, photo_id, synced_at) "
            "VALUES (?,?,?,?)",
            (space, person_id, photo_id, store.now()),
        )

    def insert_face(space, photo_id, x, y, w, h, detector="scrfd", crop_path=None):
        cur = conn.execute(
            "INSERT INTO faces (space, photo_id, detector, x, y, w, h, "
            "crop_path, pipeline_version, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (space, photo_id, detector, x, y, w, h, crop_path, "v1", store.now()),
        )
        return int(cur.lastrowid)

    def insert_embedding(face_id, model, vec, variant="orig"):
        vec = np.asarray(vec, dtype=np.float32)
        conn.execute(
            "INSERT INTO embeddings (face_id, model, variant, dim, vec, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (face_id, model, variant, int(vec.shape[0]), store.vec_to_blob(vec), store.now()),
        )

    def insert_syno_face(space, syno_face_id, photo_id, person_id, box):
        x1, y1, x2, y2 = box
        conn.execute(
            "INSERT INTO syno_faces (space, syno_face_id, photo_id, person_id, "
            "x1, y1, x2, y2, synced_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (space, syno_face_id, photo_id, person_id, x1, y1, x2, y2, store.now()),
        )

    conn.commit()
    ns = SimpleNamespace(
        conn=conn,
        insert_photo=insert_photo,
        insert_person=insert_person,
        insert_person_photo=insert_person_photo,
        insert_face=insert_face,
        insert_embedding=insert_embedding,
        insert_syno_face=insert_syno_face,
        commit=conn.commit,
    )
    yield ns
    conn.close()
