"""Tests for web config editing (configio) + the access (auth) API routes.

Fully hermetic: the tomlkit round-trip / validation / masking functions are
exercised directly against a tmp ``config.toml`` (pinned via ``SYNOPTICON_CONFIG``
so the repo's own ``data/config.toml`` is never touched), and the routes run over
a ``TestClient`` with a stubbed JobManager — the same fixture style as
``test_web_api.py``.
"""

from __future__ import annotations

import json
import stat
import sys

import pytest

from synopticon.config import load_settings
from synopticon.db import store
from synopticon.web import auth

pytest.importorskip("tomlkit")
pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from synopticon.web.app import create_app  # noqa: E402
from synopticon.web.configio import (  # noqa: E402
    config_target,
    read_config,
    write_config,
)
from synopticon.web.jobs import JobManager  # noqa: E402


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def cfg_path(tmp_path, monkeypatch):
    """Pin the config file to a tmp path so tests never touch repo config.toml."""
    p = tmp_path / "config.toml"
    monkeypatch.setenv("SYNOPTICON_CONFIG", str(p))
    return p


@pytest.fixture
def settings(tmp_path):
    return load_settings(
        storage={"data_dir": tmp_path},
        nas={"url": "https://nas.test", "account": "svc", "password": "pw"},
    )


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


def _seed_user(settings, username="admin", password="password123"):
    c = store.connect(settings.storage.db_path)
    try:
        uid = auth.create_user(c, username, password)
    finally:
        c.close()
    return uid


def _login(client, username="admin", password="password123"):
    return client.post("/login", data={"username": username, "password": password})


# --------------------------------------------------------------------------- #
# read_config: masking + schema + env overrides
# --------------------------------------------------------------------------- #
def test_read_config_masks_password(cfg_path):
    settings = load_settings(nas={"password": "super-secret"})
    data = read_config(settings)
    assert data["values"]["nas"]["password"] == {"secret": True, "set": True}
    # the plaintext must never appear anywhere in the GET payload
    assert "super-secret" not in json.dumps(data)


def test_read_config_marks_unset_password(cfg_path):
    settings = load_settings(nas={"password": ""})
    data = read_config(settings)
    assert data["values"]["nas"]["password"] == {"secret": True, "set": False}


def test_read_config_includes_schema_and_path(cfg_path):
    settings = load_settings()
    data = read_config(settings)
    assert data["path"] == str(cfg_path)
    assert data["exists"] is False
    assert "properties" in data["schema"]


def test_env_shadow_detection(cfg_path, monkeypatch):
    monkeypatch.setenv("SYNOPTICON_NAS__PASSWORD", "from-env")
    monkeypatch.setenv("SYNOPTICON_CLUSTERING__KNN_K", "128")
    settings = load_settings()
    overrides = set(read_config(settings)["env_overrides"])
    assert "nas.password" in overrides
    assert "clustering.knn_k" in overrides
    assert "nas.url" not in overrides


def test_env_shadow_detection_from_dotenv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SYNOPTICON_CONFIG", raising=False)
    (tmp_path / ".env").write_text("SYNOPTICON_NAS__ACCOUNT=fromdotenv\n")
    settings = load_settings()
    assert "nas.account" in read_config(settings)["env_overrides"]


# --------------------------------------------------------------------------- #
# write_config: round-trip, secrets, validation, atomic write, perms
# --------------------------------------------------------------------------- #
def test_write_preserves_comments_and_order(cfg_path):
    cfg_path.write_text(
        "# top comment\n"
        "[clustering]\n"
        "knn_k = 64  # inline comment\n"
        "edge_threshold = 0.5\n"
    )
    settings = load_settings()
    assert write_config(settings, {"clustering": {"knn_k": 128}}) is None
    text = cfg_path.read_text()
    assert "# top comment" in text
    assert "# inline comment" in text
    assert "knn_k = 128" in text
    assert "edge_threshold = 0.5" in text


def test_unchanged_secret_keeps_stored_value(cfg_path):
    cfg_path.write_text('[nas]\nurl = "https://old"\npassword = "stored-secret"\n')
    settings = load_settings()
    result = write_config(
        settings,
        {"nas": {"password": "__unchanged__", "url": "https://new"}},
    )
    assert result is None
    text = cfg_path.read_text()
    assert 'password = "stored-secret"' in text
    assert 'url = "https://new"' in text


def test_masked_secret_echo_is_dropped(cfg_path):
    cfg_path.write_text('[nas]\npassword = "stored-secret"\n')
    settings = load_settings()
    # a client echoing the GET marker back must not wipe the stored secret
    write_config(settings, {"nas": {"password": {"secret": True, "set": True}}})
    assert 'password = "stored-secret"' in cfg_path.read_text()


def test_changed_secret_is_written(cfg_path):
    cfg_path.write_text('[nas]\npassword = "old"\n')
    settings = load_settings()
    write_config(settings, {"nas": {"password": "brand-new"}})
    assert 'password = "brand-new"' in cfg_path.read_text()


def test_validation_error_shape(cfg_path):
    settings = load_settings()
    errors = write_config(settings, {"clustering": {"knn_k": "not-a-number"}})
    assert isinstance(errors, list) and errors
    err = errors[0]
    assert set(err) == {"loc", "msg"}
    assert err["loc"] == "clustering.knn_k"
    assert isinstance(err["msg"], str)
    # nothing is written when validation fails
    assert not cfg_path.exists()


def test_write_creates_fresh_file_at_default_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SYNOPTICON_CONFIG", raising=False)
    data_dir = tmp_path / "data"
    settings = load_settings(storage={"data_dir": data_dir})
    target = config_target(settings)
    assert target == data_dir / "config.toml"
    assert write_config(settings, {"clustering": {"knn_k": 99}}) is None
    assert target.is_file()
    assert "knn_k = 99" in target.read_text()


def test_password_write_tightens_permissions(cfg_path):
    settings = load_settings()
    write_config(settings, {"nas": {"url": "https://x", "password": "secret"}})
    mode = stat.S_IMODE(cfg_path.stat().st_mode)
    assert mode == 0o600


# --------------------------------------------------------------------------- #
# Routes: PUT /api/config gating, access (password + API keys)
# --------------------------------------------------------------------------- #
def test_put_config_409_while_job_running(client, settings, monkeypatch, cfg_path):
    _seed_user(settings)
    _login(client)
    monkeypatch.setattr(
        client.app.state.job_manager,
        "list_jobs",
        lambda: [{"state": "running", "id": "x", "name": "sync"}],
    )
    r = client.put("/api/config", json={"clustering": {"knn_k": 128}})
    assert r.status_code == 409


def test_put_config_422_on_validation_error(client, settings, cfg_path):
    _seed_user(settings)
    _login(client)
    r = client.put("/api/config", json={"clustering": {"knn_k": "nope"}})
    assert r.status_code == 422
    body = r.json()
    assert body["errors"][0]["loc"] == "clustering.knn_k"


def test_put_config_success(client, settings, cfg_path):
    _seed_user(settings)
    _login(client)
    r = client.put("/api/config", json={"clustering": {"knn_k": 77}})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert "knn_k = 77" in cfg_path.read_text()


def test_get_config_route(client, settings, cfg_path):
    _seed_user(settings)
    _login(client)
    r = client.get("/api/config")
    assert r.status_code == 200
    data = r.json()
    assert data["values"]["nas"]["password"]["secret"] is True
    assert "pw" not in r.text


def test_change_password_flow(client, settings):
    _seed_user(settings)
    _login(client)
    # wrong current password -> 403
    r = client.post(
        "/api/auth/change-password",
        json={"current_password": "wrong", "new_password": "newpass456"},
    )
    assert r.status_code == 403
    # correct current password -> 200 and the new password verifies
    r = client.post(
        "/api/auth/change-password",
        json={"current_password": "password123", "new_password": "newpass456"},
    )
    assert r.status_code == 200
    c = store.connect(settings.storage.db_path)
    try:
        assert auth.verify_password(c, "admin", "newpass456") is not None
        assert auth.verify_password(c, "admin", "password123") is None
    finally:
        c.close()


def test_change_password_requires_new(client, settings):
    _seed_user(settings)
    _login(client)
    r = client.post(
        "/api/auth/change-password",
        json={"current_password": "password123", "new_password": ""},
    )
    assert r.status_code == 422


def test_api_key_create_list_revoke(client, settings):
    _seed_user(settings)
    _login(client)
    r = client.post("/api/auth/keys", json={"name": "ci"})
    assert r.status_code == 201
    key = r.json()["key"]
    assert key.startswith("syn_")

    listing = client.get("/api/auth/keys").json()["keys"]
    assert len(listing) == 1
    kid = listing[0]["id"]
    assert listing[0]["name"] == "ci"
    assert listing[0]["revoked"] is False
    # the stored hash / plaintext is never listed
    assert "key_hash" not in listing[0]
    assert key not in json.dumps(listing)

    # drop the session cookie so the Bearer key is the only credential
    client.cookies.clear()
    auth_header = {"Authorization": f"Bearer {key}"}
    # the fresh key authenticates the API on its own
    assert client.get("/api/stats", headers=auth_header).status_code == 200
    # revoke (authenticated via the key itself) -> key is then rejected
    assert client.post(
        f"/api/auth/keys/{kid}/revoke", json={}, headers=auth_header
    ).status_code == 200
    assert client.get("/api/stats", headers=auth_header).status_code == 401
    # a re-login shows the key marked revoked
    _login(client)
    assert client.get("/api/auth/keys").json()["keys"][0]["revoked"] is True


def test_api_key_create_requires_name(client, settings):
    _seed_user(settings)
    _login(client)
    r = client.post("/api/auth/keys", json={"name": "  "})
    assert r.status_code == 422
