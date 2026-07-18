"""Setup-wizard API: /api/setup/status, /test-connection, /check-storage.

TestClient over ``create_app`` with a seeded tmp DB (mirrors test_web_api's
fixture style). No real NAS: the connectivity probe's HTTP is respx-mocked, and
because the app is driven through an ASGI transport it is not intercepted by the
NAS-facing respx router.
"""

from __future__ import annotations

import sys

import httpx
import pytest
import respx

from synopticon.config import load_settings
from synopticon.db import store
from synopticon.web import auth

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from synopticon.web.app import create_app  # noqa: E402
from synopticon.web.jobs import JobManager  # noqa: E402
from tests.unit.conftest import NAS_BASE_URL  # noqa: E402

API_INFO = {
    "success": True,
    "data": {
        "SYNO.API.Auth": {"minVersion": 1, "maxVersion": 6, "path": "auth.cgi"},
        "SYNO.Foto.Browse.Person": {"minVersion": 1, "maxVersion": 3, "path": "entry.cgi"},
        "SYNO.Foto.Browse.Item": {"minVersion": 1, "maxVersion": 7, "path": "entry.cgi"},
    },
}
LOGIN_OK = {"success": True, "data": {"sid": "sid-1", "synotoken": "tok-1", "did": "did-1"}}


@pytest.fixture
def settings(tmp_path):
    return load_settings(
        storage={"data_dir": tmp_path, "models_dir": tmp_path / "models"},
        nas={"url": NAS_BASE_URL, "account": "svc", "password": "pw"},
    )


@pytest.fixture
def db(settings):
    c = store.connect(settings.storage.db_path)
    yield c
    c.close()


def _trivial_builder(argv):
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


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #
ALL_MODEL_KEYS = {
    "scrfd_10g_bnkps",
    "yolov8l-face",
    "glintr100",
    "adaface_ir101_webface12m",
    "magface_iresnet100",
}
MODEL_FILES = {
    "scrfd_10g_bnkps": "scrfd_10g_bnkps.onnx",
    "yolov8l-face": "yolov8l-face.onnx",
    "glintr100": "glintr100.onnx",
    "adaface_ir101_webface12m": "adaface_ir101_webface12m.onnx",
    "magface_iresnet100": "magface_iresnet100.onnx",
}


def _write_weights(models_dir, keys):
    """Drop a dummy weight file on disk for each key (disk presence is what counts)."""
    models_dir.mkdir(parents=True, exist_ok=True)
    for key in keys:
        (models_dir / MODEL_FILES[key]).write_bytes(b"weights")


def test_status_shape_on_fresh_db(client):
    r = client.get("/api/setup/status")
    assert r.status_code == 200
    data = r.json()
    # nas creds are present in the fixture settings -> configured; but no weights,
    # no account, and every pipeline table is empty.
    assert data["nas_configured"] is True
    assert data["models_ready"] is False
    # A fresh install (no weights on disk) reports all five required models missing.
    assert set(data["models_missing"]) == ALL_MODEL_KEYS
    assert data["account_created"] is False
    assert data["photos_synced"] == 0
    assert data["extract_done"] == 0
    assert data["cluster_runs"] == 0
    assert "config_file" in data
    assert data["storage"]["data_dir"]


def test_models_ready_when_all_weight_files_present(client, settings):
    # Create all five dummy weight files. No manifest entries are written, proving
    # that on-disk presence — not manifest registration — is what decides ready.
    _write_weights(settings.storage.models_dir, ALL_MODEL_KEYS)
    data = client.get("/api/setup/status").json()
    assert data["models_ready"] is True
    assert data["models_missing"] == []


def test_models_partial_reports_missing_keys(client, settings):
    present = {"scrfd_10g_bnkps", "glintr100", "yolov8l-face"}
    _write_weights(settings.storage.models_dir, present)
    data = client.get("/api/setup/status").json()
    assert data["models_ready"] is False
    assert set(data["models_missing"]) == ALL_MODEL_KEYS - present


def test_status_reachable_pre_auth_on_first_boot(client):
    # No users yet -> first boot -> reachable with no session.
    assert client.get("/api/setup/status").status_code == 200


def test_setup_requires_auth_after_account_created(app, db):
    auth.create_user(db, "admin", "password123")
    with TestClient(app, follow_redirects=False) as c:
        # A fresh client (no cookie) is now rejected like any other API.
        assert c.get("/api/setup/status").status_code == 401


# --------------------------------------------------------------------------- #
# check-storage
# --------------------------------------------------------------------------- #
def test_check_storage_writable(client, tmp_path):
    r = client.post(
        "/api/setup/check-storage",
        json={
            "data_dir": str(tmp_path / "d"),
            "models_dir": str(tmp_path / "m"),
        },
    )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["dirs"]["data_dir"]["ok"] is True
    assert data["dirs"]["data_dir"]["free_gb"] is not None


def test_check_storage_not_writable(client, tmp_path):
    r = client.post(
        "/api/setup/check-storage",
        json={
            "data_dir": str(tmp_path / "ok"),
            # A subdir of /proc cannot be created (read-only pseudo-filesystem).
            "models_dir": "/proc/synopticon_setup_no_write",
        },
    )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is False
    assert data["dirs"]["models_dir"]["ok"] is False


# --------------------------------------------------------------------------- #
# test-connection (respx-mocked NAS)
# --------------------------------------------------------------------------- #
def test_test_connection_success(client):
    with respx.mock(assert_all_called=False, assert_all_mocked=False) as router:
        router.post(f"{NAS_BASE_URL}/webapi/query.cgi").mock(
            return_value=httpx.Response(200, json=API_INFO)
        )
        router.post(f"{NAS_BASE_URL}/webapi/auth.cgi").mock(
            return_value=httpx.Response(200, json=LOGIN_OK)
        )
        r = client.post(
            "/api/setup/test-connection",
            json={
                "url": NAS_BASE_URL,
                "account": "svc",
                "password": "pw",
                "verify_tls": True,
                "spaces": ["personal"],
            },
        )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert [s["name"] for s in data["steps"]] == ["reachable", "login", "photos"]
    assert data["person_api"] == "SYNO.Foto.Browse.Person"


def test_test_connection_bad_credentials(client):
    with respx.mock(assert_all_called=False, assert_all_mocked=False) as router:
        router.post(f"{NAS_BASE_URL}/webapi/query.cgi").mock(
            return_value=httpx.Response(200, json=API_INFO)
        )
        router.post(f"{NAS_BASE_URL}/webapi/auth.cgi").mock(
            return_value=httpx.Response(200, json={"success": False, "error": {"code": 400}})
        )
        r = client.post(
            "/api/setup/test-connection",
            json={"url": NAS_BASE_URL, "account": "svc", "password": "bad"},
        )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is False
    assert data["steps"][-1]["name"] == "login"
    assert data["steps"][-1]["ok"] is False
