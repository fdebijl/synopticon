"""SynoClient: version discovery/caching, pagination, retry, session re-login, errors."""

from __future__ import annotations

import urllib.parse

import httpx
import pytest

from synopticon.db import store
from synopticon.syno.client import SynoApiError, SynoClient, SynoVersionError
from tests.unit.conftest import NAS_BASE_URL

API_INFO = {
    "success": True,
    "data": {
        "SYNO.API.Auth": {"minVersion": 1, "maxVersion": 6, "path": "auth.cgi"},
        "SYNO.Foto.Browse.Person": {"minVersion": 1, "maxVersion": 3, "path": "entry.cgi"},
        "SYNO.Foto.Browse.Item": {"minVersion": 1, "maxVersion": 7, "path": "entry.cgi"},
    },
}
LOGIN_OK = {"success": True, "data": {"sid": "sid-1", "synotoken": "tok-1", "did": "did-1"}}


def _form(request: httpx.Request) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(request.content.decode()))


def _bootstrap(respx_mock):
    info_route = respx_mock.post(f"{NAS_BASE_URL}/webapi/query.cgi").mock(
        return_value=httpx.Response(200, json=API_INFO)
    )
    login_route = respx_mock.post(f"{NAS_BASE_URL}/webapi/auth.cgi").mock(
        return_value=httpx.Response(200, json=LOGIN_OK)
    )
    return info_route, login_route


@pytest.fixture
def client(nas_settings, nas_conn):
    c = SynoClient(nas_settings, nas_conn)
    yield c
    c.close()


def test_discovery_populates_cache_and_reuses_it(respx_mock, client, nas_conn, nas_settings):
    info_route, _ = _bootstrap(respx_mock)

    info = client.api_info
    assert info["SYNO.Foto.Browse.Person"]["maxVersion"] == 3
    assert info_route.call_count == 1
    assert store.get_state(nas_conn, "api_info")["SYNO.Foto.Browse.Person"]["maxVersion"] == 3

    # A second client against the same conn must reuse the sync_state cache:
    # no second query.cgi hit even though the route is still registered.
    client2 = SynoClient(nas_settings, nas_conn)
    info2 = client2.api_info
    assert info2 == info
    assert info_route.call_count == 1
    client2.close()


def test_version_for_clamps_to_max_and_raises_below_min(respx_mock, client):
    _bootstrap(respx_mock)
    assert client.version_for("SYNO.Foto.Browse.Person", 99) == 3
    with pytest.raises(SynoVersionError):
        client.version_for("SYNO.API.Auth", 0)


def test_paginate_terminates_on_short_page(respx_mock, client):
    _bootstrap(respx_mock)
    calls: list[tuple[int, int]] = []

    def _list(request):
        body = _form(request)
        offset, limit = int(body["offset"]), int(body["limit"])
        calls.append((offset, limit))
        items = [{"id": 0}, {"id": 1}] if offset == 0 else [{"id": 2}]
        return httpx.Response(200, json={"success": True, "data": {"list": items}})

    respx_mock.post(f"{NAS_BASE_URL}/webapi/entry.cgi").mock(side_effect=_list)

    items = list(client.paginate("SYNO.Foto.Browse.Item", "list", page_size=2))
    assert [i["id"] for i in items] == [0, 1, 2]
    assert calls == [(0, 2), (2, 2)]


def test_paginate_terminates_on_empty_first_page(respx_mock, client):
    _bootstrap(respx_mock)
    respx_mock.post(f"{NAS_BASE_URL}/webapi/entry.cgi").mock(
        return_value=httpx.Response(200, json={"success": True, "data": {"list": []}})
    )
    items = list(client.paginate("SYNO.Foto.Browse.Item", "list", page_size=2))
    assert items == []


def test_retry_on_5xx_then_success(respx_mock, client):
    _bootstrap(respx_mock)
    route = respx_mock.post(f"{NAS_BASE_URL}/webapi/entry.cgi").mock(
        side_effect=[
            httpx.Response(500),
            httpx.Response(200, json={"success": True, "data": {"ok": True}}),
        ]
    )
    data = client.call("SYNO.Foto.Browse.Item", "get", id=[1])
    assert data == {"ok": True}
    assert route.call_count == 2


def test_relogin_on_session_expired_code(respx_mock, client, nas_conn):
    _, login_route = _bootstrap(respx_mock)
    api_route = respx_mock.post(f"{NAS_BASE_URL}/webapi/entry.cgi").mock(
        side_effect=[
            httpx.Response(200, json={"success": False, "error": {"code": 105}}),
            httpx.Response(200, json={"success": True, "data": {"list": []}}),
        ]
    )
    data = client.call("SYNO.Foto.Browse.Item", "list", offset=0, limit=10)
    assert data == {"list": []}
    assert api_route.call_count == 2
    # One login on first use + one re-login triggered by the 105.
    assert login_route.call_count == 2
    assert store.get_state(nas_conn, "auth_did") == "did-1"


def test_unhandled_error_code_raises_synoapierror(respx_mock, client):
    _bootstrap(respx_mock)
    respx_mock.post(f"{NAS_BASE_URL}/webapi/entry.cgi").mock(
        return_value=httpx.Response(200, json={"success": False, "error": {"code": 999}})
    )
    with pytest.raises(SynoApiError) as exc_info:
        client.call("SYNO.Foto.Browse.Item", "list", offset=0, limit=10)
    assert exc_info.value.code == 999
