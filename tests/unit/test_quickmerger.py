"""QuickMerger API: NAS proxying, consent gates and the named-source merge refusal.

Hermetic: the NAS is respx-mocked (TestClient's own transport is a different
class, so it is never intercepted), and the app is built with the standard
stub dist + a JobManager that never spawns anything.
"""

from __future__ import annotations

import urllib.parse

import httpx
import pytest

from synopticon.config import load_settings
from synopticon.db import store
from synopticon.web import auth
from tests.unit.conftest import NAS_BASE_URL

pytest.importorskip("fastapi")
pytest.importorskip("respx")
from fastapi.testclient import TestClient  # noqa: E402

from synopticon.web.app import create_app  # noqa: E402
from synopticon.web.jobs import JobManager  # noqa: E402

API_INFO = {
    "success": True,
    "data": {
        "SYNO.API.Auth": {"minVersion": 1, "maxVersion": 6, "path": "auth.cgi"},
        "SYNO.Foto.Browse.Person": {"minVersion": 1, "maxVersion": 3, "path": "entry.cgi"},
        "SYNO.Foto.Thumbnail": {"minVersion": 1, "maxVersion": 2, "path": "entry.cgi"},
    },
}
LOGIN_OK = {"success": True, "data": {"sid": "sid-1", "synotoken": "tok-1", "did": "did-1"}}


def _person(pid: int, name: str = "", count: int = 3) -> dict:
    return {
        "id": pid,
        "name": name,
        "item_count": count,
        "show": True,
        "cover": pid * 10,
        "additional": {"thumbnail": {"cache_key": f"ck-{pid}"}},
    }


@pytest.fixture
def settings(tmp_path):
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
def db(settings):
    c = store.connect(settings.storage.db_path)
    yield c
    c.close()


@pytest.fixture
def app(settings, tmp_path, stub_dist):
    jm = JobManager(tmp_path / "jobs", command_builder=lambda argv: ["/bin/true"])
    application = create_app(settings, job_manager=jm, dist_dir=stub_dist)
    yield application
    jm.shutdown()


@pytest.fixture
def client(app, db):
    auth.create_user(db, "admin", "password123")
    with TestClient(app, follow_redirects=False) as c:
        c.post("/api/auth/login", json={"username": "admin", "password": "password123"})
        yield c


class _Nas:
    """Dispatches mocked entry.cgi POSTs by their `method` param, recording forms."""

    def __init__(self, respx_mock, handlers: dict):
        self.handlers = handlers
        self.calls: list[dict] = []
        respx_mock.post(f"{NAS_BASE_URL}/webapi/query.cgi").mock(
            return_value=httpx.Response(200, json=API_INFO)
        )
        respx_mock.post(f"{NAS_BASE_URL}/webapi/auth.cgi").mock(
            return_value=httpx.Response(200, json=LOGIN_OK)
        )
        respx_mock.post(f"{NAS_BASE_URL}/webapi/entry.cgi").mock(side_effect=self._dispatch)

    def _dispatch(self, request: httpx.Request) -> httpx.Response:
        form = dict(urllib.parse.parse_qsl(request.content.decode()))
        self.calls.append(form)
        handler = self.handlers[form["method"]]
        payload = handler(form) if callable(handler) else handler
        return httpx.Response(200, json=payload)

    def forms(self, method: str) -> list[dict]:
        return [f for f in self.calls if f["method"] == method]


def _list_page(people: list[dict]):
    def handler(form):
        # Second page is empty, which is what ends `paginate`.
        return {
            "success": True,
            "data": {"list": people if form.get("offset") == "0" else []},
        }

    return handler


# --------------------------------------------------------------------------- #
# read
# --------------------------------------------------------------------------- #
def test_persons_lists_only_unnamed_and_caches(client, respx_mock):
    nas = _Nas(respx_mock, {"list": _list_page([_person(1), _person(2, "Alice"), _person(3)])})

    data = client.get("/api/quickmerger/persons").json()
    assert [p["id"] for p in data["persons"]] == [1, 3]
    assert data["cached"] is False
    first = data["persons"][0]
    assert first["thumb_url"] == "/api/quickmerger/thumb?space=personal&id=1&cache_key=ck-1"
    assert first["link"].endswith("#/person/personal_space/1")
    # The long-tail listing is what the whole tool is about.
    assert nas.forms("list")[0]["show_more"] == "true"

    cached = client.get("/api/quickmerger/persons").json()
    assert cached["cached"] is True
    assert len(nas.forms("list")) == 1  # served from cache, not re-fetched


def test_persons_refresh_bypasses_cache(client, respx_mock):
    nas = _Nas(respx_mock, {"list": _list_page([_person(1)])})
    client.get("/api/quickmerger/persons")
    client.get("/api/quickmerger/persons?refresh=true")
    assert len(nas.forms("list")) == 2


def test_persons_rejects_unknown_space(client, respx_mock):
    _Nas(respx_mock, {})
    assert client.get("/api/quickmerger/persons?space=nope").status_code == 422


def test_suggest_proxies_prefix(client, respx_mock):
    nas = _Nas(
        respx_mock,
        {"suggest": {"success": True, "data": {"list": [_person(9, "Alice")]}}},
    )
    data = client.get("/api/quickmerger/suggest?prefix=Al").json()
    assert data["suggestions"][0] == {
        "id": 9,
        "space": "personal",
        "name": "Alice",
        "item_count": 3,
        "thumb_url": "/api/quickmerger/thumb?space=personal&id=9&cache_key=ck-9",
        "link": f"{NAS_BASE_URL}/?launchApp=SYNO.Foto.AppInstance#/person/personal_space/9",
    }
    assert nas.forms("suggest")[0]["name_prefix"] == '"Al"'


def test_suggest_empty_prefix_does_not_hit_nas(client, respx_mock):
    nas = _Nas(respx_mock, {})
    assert client.get("/api/quickmerger/suggest?prefix=  ").json() == {"suggestions": []}
    assert nas.calls == []


def test_thumb_proxies_bytes_privately(client, respx_mock):
    _Nas(respx_mock, {})
    respx_mock.get(f"{NAS_BASE_URL}/webapi/entry.cgi").mock(
        return_value=httpx.Response(
            200, content=b"\xff\xd8jpeg", headers={"content-type": "image/jpeg"}
        )
    )
    r = client.get("/api/quickmerger/thumb?space=personal&id=1&cache_key=ck-1")
    assert r.status_code == 200
    assert r.content == b"\xff\xd8jpeg"
    # Never `no-store` (the /api default) and never shared-cacheable.
    assert r.headers["cache-control"] == "private, max-age=86400"


def test_thumb_requires_cache_key(client, respx_mock):
    _Nas(respx_mock, {})
    assert client.get("/api/quickmerger/thumb?id=1").status_code == 422


def test_nas_failure_is_502_not_500(client, respx_mock):
    _Nas(respx_mock, {"list": {"success": False, "error": {"code": 119}}})
    r = client.get("/api/quickmerger/persons")
    assert r.status_code == 502
    assert r.json()["code"] == 119


# --------------------------------------------------------------------------- #
# consent
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "path,body",
    [
        ("/api/quickmerger/name", {"person_id": 1, "name": "Alice"}),
        ("/api/quickmerger/hide", {"person_id": 1}),
        ("/api/quickmerger/merge", {"source_id": 1, "target_id": 2}),
    ],
)
def test_writes_without_confirm_are_428(client, respx_mock, path, body):
    nas = _Nas(respx_mock, {})
    r = client.post(path, json=body)
    assert r.status_code == 428
    assert r.json()["requirement"] == "confirm"
    assert nas.calls == []


# --------------------------------------------------------------------------- #
# writes
# --------------------------------------------------------------------------- #
def test_name_writes_audits_and_mirrors(client, db, respx_mock):
    nas = _Nas(respx_mock, {"set": {"success": True, "data": {}}})
    db.execute(
        "INSERT INTO persons (id, space, name, synced_at) VALUES (1, 'personal', NULL, 0)"
    )
    db.commit()

    r = client.post(
        "/api/quickmerger/name", json={"person_id": 1, "name": "Alice", "confirm": True}
    )
    assert r.status_code == 200
    assert nas.forms("set")[0]["name"] == '"Alice"'

    row = db.execute("SELECT name FROM persons WHERE space='personal' AND id=1").fetchone()
    assert row["name"] == "Alice"
    actions = [
        r["action"] for r in db.execute("SELECT action FROM audit_log").fetchall()
    ]
    assert actions == ["quickmerger.rename"]


def test_hide_writes_show_false_and_mirrors(client, db, respx_mock):
    nas = _Nas(respx_mock, {"show": {"success": True, "data": {}}})
    db.execute(
        "INSERT INTO persons (id, space, name, show, synced_at) "
        "VALUES (1, 'personal', NULL, 1, 0)"
    )
    db.commit()

    assert client.post("/api/quickmerger/hide", json={"person_id": 1, "confirm": True}).status_code == 200
    form = nas.forms("show")[0]
    assert form["id"] == "[1]" and form["show"] == "false"
    row = db.execute("SELECT show FROM persons WHERE space='personal' AND id=1").fetchone()
    assert row["show"] == 0


def test_merge_of_unnamed_into_named(client, db, respx_mock):
    def get_person(form):
        pid = int(form["id"].strip("[]"))
        return {
            "success": True,
            "data": {"list": [_person(pid, "" if pid == 1 else "Alice")]},
        }

    nas = _Nas(respx_mock, {"get": get_person, "merge": {"success": True, "data": {}}})
    db.execute("INSERT INTO persons (id, space, name, synced_at) VALUES (1,'personal',NULL,0)")
    db.commit()

    r = client.post(
        "/api/quickmerger/merge",
        json={"source_id": 1, "target_id": 2, "confirm": True},
    )
    assert r.status_code == 200
    form = nas.forms("merge")[0]
    assert form["target_id"] == "2" and form["merged_id"] == "[1]" and form["name"] == '"Alice"'

    row = db.execute("SELECT deleted FROM persons WHERE space='personal' AND id=1").fetchone()
    assert row["deleted"] == 1
    actions = [r["action"] for r in db.execute("SELECT action FROM audit_log").fetchall()]
    assert actions == ["quickmerger.merge"]


def test_merge_refuses_named_source(client, respx_mock):
    nas = _Nas(
        respx_mock,
        {
            "get": lambda form: {
                "success": True,
                "data": {"list": [_person(int(form["id"].strip("[]")), "Bob")]},
            },
            "merge": {"success": True, "data": {}},
        },
    )
    r = client.post(
        "/api/quickmerger/merge",
        json={"source_id": 1, "target_id": 2, "confirm": True},
    )
    assert r.status_code == 409
    assert r.json()["requirement"] == "unnamed_source"
    assert nas.forms("merge") == []  # never reached the NAS


def test_merge_into_self_is_422(client, respx_mock):
    nas = _Nas(respx_mock, {})
    r = client.post(
        "/api/quickmerger/merge", json={"source_id": 5, "target_id": 5, "confirm": True}
    )
    assert r.status_code == 422
    assert nas.calls == []


def test_writes_require_authentication(app, db, respx_mock):
    auth.create_user(db, "admin", "password123")
    _Nas(respx_mock, {})
    with TestClient(app, follow_redirects=False) as c:
        assert c.post("/api/quickmerger/merge", json={"confirm": True}).status_code == 401
        assert c.get("/api/quickmerger/persons").status_code == 401
