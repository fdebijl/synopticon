"""NAS connectivity probe + `check` CLI rendering.

respx-mocked SYNO.API.Info / SYNO.API.Auth (same pattern as test_client). The
`check` command is exercised directly with its settings/conn factories
monkeypatched, and its stdout/stderr asserted byte-for-byte against the format
the pre-refactor inline implementation produced.
"""

from __future__ import annotations

import httpx
import pytest
import typer

from synopticon import cli
from synopticon.syno.probe import probe
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
BAD_LOGIN = {"success": False, "error": {"code": 400}}


def _mock_info(respx_mock):
    respx_mock.post(f"{NAS_BASE_URL}/webapi/query.cgi").mock(
        return_value=httpx.Response(200, json=API_INFO)
    )


def _mock_login(respx_mock, envelope=LOGIN_OK):
    respx_mock.post(f"{NAS_BASE_URL}/webapi/auth.cgi").mock(
        return_value=httpx.Response(200, json=envelope)
    )


# --------------------------------------------------------------------------- #
# probe()
# --------------------------------------------------------------------------- #
def test_probe_success_all_steps_ok(respx_mock, nas_settings, nas_conn):
    _mock_info(respx_mock)
    _mock_login(respx_mock)

    result = probe(nas_settings, nas_conn)

    assert result.ok is True
    assert result.error is None
    assert [s.name for s in result.steps] == ["reachable", "login", "photos"]
    assert all(s.ok for s in result.steps)
    assert result.api_count == 3
    assert result.synotoken is True
    assert result.device_token is True
    assert result.person_api == "SYNO.Foto.Browse.Person"
    assert result.person_api_version == 3

    d = result.to_dict()
    assert d["ok"] is True
    assert len(d["steps"]) == 3
    assert d["steps"][0] == {"name": "reachable", "ok": True, "detail": "3 APIs discovered"}


def test_probe_unreachable_marks_reachable_step_failed(
    respx_mock, nas_settings, nas_conn, monkeypatch
):
    # Fail fast: one attempt, no retry backoff sleeps.
    monkeypatch.setattr("synopticon.syno.client._RETRY_ATTEMPTS", 1)
    respx_mock.post(f"{NAS_BASE_URL}/webapi/query.cgi").mock(
        side_effect=httpx.ConnectError("no route to host")
    )

    result = probe(nas_settings, nas_conn)

    assert result.ok is False
    assert result.error
    assert [s.name for s in result.steps] == ["reachable"]
    assert result.steps[-1].ok is False
    assert result.api_count is None


def test_probe_bad_credentials_marks_login_step_failed(respx_mock, nas_settings, nas_conn):
    _mock_info(respx_mock)
    _mock_login(respx_mock, BAD_LOGIN)

    result = probe(nas_settings, nas_conn)

    assert result.ok is False
    assert [s.name for s in result.steps] == ["reachable", "login"]
    assert result.steps[0].ok is True
    assert result.steps[1].ok is False
    assert "400" in (result.error or "")


# --------------------------------------------------------------------------- #
# `check` CLI output — must stay byte-identical to the pre-refactor format
# --------------------------------------------------------------------------- #
def test_check_output_success_unchanged(
    respx_mock, nas_settings, nas_conn, monkeypatch, capsys
):
    _mock_info(respx_mock)
    _mock_login(respx_mock)
    monkeypatch.setattr(cli, "_settings", lambda: nas_settings)
    monkeypatch.setattr(cli, "_conn", lambda s: nas_conn)

    cli.check()

    out = capsys.readouterr().out
    assert out == (
        "NAS url:  https://nas.test\n"
        "account:  svc\n"
        "reachable: yes (3 APIs discovered)\n"
        "login:     OK (synotoken=yes, 2FA device token stored)\n"
        "photos:    SYNO.Foto.Browse.Person available at v3\n"
        "all good — you can run: synopticon sync\n"
    )


def test_check_output_failure_unchanged(
    respx_mock, nas_settings, nas_conn, monkeypatch, capsys
):
    _mock_info(respx_mock)
    _mock_login(respx_mock, BAD_LOGIN)
    monkeypatch.setattr(cli, "_settings", lambda: nas_settings)
    monkeypatch.setattr(cli, "_conn", lambda s: nas_conn)

    with pytest.raises(typer.Exit) as excinfo:
        cli.check()
    assert excinfo.value.exit_code == 1

    captured = capsys.readouterr()
    # Partial progress (the steps that passed) is still printed to stdout.
    assert captured.out == (
        "NAS url:  https://nas.test\n"
        "account:  svc\n"
        "reachable: yes (3 APIs discovered)\n"
    )
    assert captured.err.startswith("\nFAILED:")
    assert "400" in captured.err
