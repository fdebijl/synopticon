"""DryRunWriter / SynoWriter / apply_reviewed against respx-mocked HAR-verified payloads."""

from __future__ import annotations

import json
import urllib.parse

import httpx
import pytest

from synopticon import audit
from synopticon.db import store
from synopticon.syno import writeback
from synopticon.syno.client import SynoClient
from tests.unit.conftest import NAS_BASE_URL

API_INFO = {
    "success": True,
    "data": {
        "SYNO.API.Auth": {"minVersion": 1, "maxVersion": 6, "path": "auth.cgi"},
        "SYNO.Foto.Browse.Item": {"minVersion": 1, "maxVersion": 7, "path": "entry.cgi"},
        "SYNO.Foto.Browse.Person": {"minVersion": 1, "maxVersion": 3, "path": "entry.cgi"},
        "SYNO.Foto.Upload.Face": {"minVersion": 1, "maxVersion": 1, "path": "entry.cgi"},
    },
}
LOGIN_OK = {"success": True, "data": {"sid": "sid-1", "synotoken": "tok-1", "did": "did-1"}}

# Verbatim from adding_face_to_photo_without_face.har entry 8's response.
ADD_FACE_RESP = {"success": True, "data": {"list": [{"face_id": 77772, "face_id_temp": "103153-0"}]}}
# Verbatim from entry 11's response (post-assign list_face).
LIST_FACE_RESP_TAGGED = {
    "success": True,
    "data": {
        "list": [
            {
                "face_bounding_box": {
                    "top_left": {"x": 0.4008430182190183, "y": 0.23976940730726903},
                    "bottom_right": {"x": 0.6994280869527612, "y": 0.46366557859071184},
                },
                "face_id": 77772,
                "name": "Floris de Bijl",
                "person_id": 2660,
                "thumbnail": {"cache_key": "84534_1738792645"},
            }
        ]
    },
}
LIST_FACE_RESP_EMPTY = {"success": True, "data": {"list": []}}


def _form(request: httpx.Request) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(request.content.decode()))


def _bootstrap(respx_mock):
    respx_mock.post(f"{NAS_BASE_URL}/webapi/query.cgi").mock(
        return_value=httpx.Response(200, json=API_INFO)
    )
    respx_mock.post(f"{NAS_BASE_URL}/webapi/auth.cgi").mock(
        return_value=httpx.Response(200, json=LOGIN_OK)
    )


@pytest.fixture
def client(nas_settings, nas_conn):
    c = SynoClient(nas_settings, nas_conn)
    yield c
    c.close()


def _insert_review_row(conn, kind: str, payload: dict, status: str = "approved") -> None:
    conn.execute(
        "INSERT INTO review_queue (kind, payload_json, status, created_at) VALUES (?, ?, ?, ?)",
        (kind, json.dumps(payload), status, store.now()),
    )
    conn.commit()


# -- DryRunWriter -------------------------------------------------------------


def test_dryrun_writer_audits_without_any_network_call(respx_mock, nas_conn):
    writer = writeback.DryRunWriter(nas_conn)

    r1 = writer.assign(1, 2, (0.1, 0.1, 0.2, 0.2))
    r2 = writer.merge(10, [11], "Foo")
    r3 = writer.rename(10, "Bar")
    r4 = writer.delete_face("personal", 5)

    assert r1.success and r2.success and r3.success and r4.success
    assert len(respx_mock.calls) == 0

    rows = audit.tail(nas_conn, limit=10)
    dryrun_rows = [r for r in rows if r["action"].startswith("dryrun.")]
    assert {r["action"] for r in dryrun_rows} == {
        "dryrun.assign", "dryrun.merge", "dryrun.rename", "dryrun.delete_face",
    }
    assert all(r["success"] == 1 for r in dryrun_rows)


# -- SynoWriter.assign full chain ---------------------------------------------


def test_synowriter_assign_full_chain(respx_mock, client, nas_conn):
    _bootstrap(respx_mock)

    def _dispatch(request):
        query = dict(request.url.params)
        if query.get("method") == "upload":
            return httpx.Response(200, json={"success": True})
        method = _form(request).get("method")
        if method == "add_face":
            return httpx.Response(200, json=ADD_FACE_RESP)
        if method == "list_face":
            return httpx.Response(200, json=LIST_FACE_RESP_TAGGED)
        raise AssertionError(f"unexpected call: method={method!r}")

    respx_mock.post(f"{NAS_BASE_URL}/webapi/entry.cgi").mock(side_effect=_dispatch)

    writer = writeback.SynoWriter(client, nas_conn, "personal")
    result = writer.assign(
        103153,
        2660,
        (0.4008430182190183, 0.23976940730726903, 0.6994280869527612, 0.46366557859071184),
        face_crop_jpeg=b"fake-jpeg-bytes",
    )
    assert result.success is True

    rows = audit.tail(nas_conn, limit=10)
    actions = sorted(r["action"] for r in rows if r["action"].startswith("writeback.assign"))
    assert actions == [
        "writeback.assign.add_face", "writeback.assign.upload_face", "writeback.assign.verify",
    ]
    assert all(r["success"] == 1 for r in rows if r["action"].startswith("writeback.assign"))


def test_synowriter_assign_add_face_failure_short_circuits(respx_mock, client, nas_conn):
    _bootstrap(respx_mock)
    respx_mock.post(f"{NAS_BASE_URL}/webapi/entry.cgi").mock(
        return_value=httpx.Response(200, json={"success": False, "error": {"code": 642}})
    )
    writer = writeback.SynoWriter(client, nas_conn, "personal")
    result = writer.assign(1, 2, (0.1, 0.1, 0.2, 0.2))
    assert result.success is False
    assert result.error_code == 642
    rows = audit.tail(nas_conn, limit=10)
    actions = [r["action"] for r in rows if r["action"].startswith("writeback.assign")]
    assert actions == ["writeback.assign.add_face"]  # no upload/verify attempted


# -- apply_reviewed ------------------------------------------------------------


def test_apply_reviewed_idempotent_skip_no_write(respx_mock, client, nas_conn):
    _bootstrap(respx_mock)

    def _dispatch(request):
        method = _form(request).get("method")
        if method == "list_face":
            return httpx.Response(200, json=LIST_FACE_RESP_TAGGED)
        raise AssertionError(f"unexpected call: method={method!r} (add_face should not fire)")

    respx_mock.post(f"{NAS_BASE_URL}/webapi/entry.cgi").mock(side_effect=_dispatch)

    writer = writeback.SynoWriter(client, nas_conn, "personal")
    _insert_review_row(
        nas_conn,
        "assign",
        {
            "face_id": 1, "photo_id": 103153, "space": "personal", "person_id": 2660,
            "person_name": None, "bbox_normalized": [0.1, 0.1, 0.2, 0.2], "confidence": 0.9,
        },
    )

    stats = writeback.apply_reviewed(nas_conn, writer, kinds=["assign"], apply_merges=False)
    assert stats.considered == 1
    assert stats.applied == 1
    assert stats.failed == 0
    row = nas_conn.execute("SELECT status FROM review_queue WHERE kind = 'assign'").fetchone()
    assert row["status"] == "applied"


def test_apply_reviewed_merge_idempotent_when_person_a_is_merged_side(
    respx_mock, client, nas_conn
):
    """Re-apply detection must probe the side _merge_order deletes, not always
    person_b. Here person_a is unnamed so it becomes the merged (deleted) side;
    on re-apply it's gone while the named person_b survives -> skip, no re-merge.
    """
    _bootstrap(respx_mock)

    def _dispatch(request):
        form = _form(request)
        method = form.get("method")
        if method == "get":
            # person_a (10) was merged away -> absent; person_b (20) survives.
            gone = "10" in (form.get("id") or "")
            data = [] if gone else [
                {"id": 20, "name": "Bob", "item_count": 1, "show": True, "cover": 1}
            ]
            return httpx.Response(200, json={"success": True, "data": {"list": data}})
        raise AssertionError(f"merge must not re-fire on re-apply: method={method!r}")

    respx_mock.post(f"{NAS_BASE_URL}/webapi/entry.cgi").mock(side_effect=_dispatch)

    writer = writeback.SynoWriter(client, nas_conn, "personal")
    _insert_review_row(
        nas_conn,
        "merge",
        {
            "person_a": {"space": "personal", "person_id": 10, "name": None},
            "person_b": {"space": "personal", "person_id": 20, "name": "Bob"},
            "evidence": {},
        },
    )

    stats = writeback.apply_reviewed(nas_conn, writer, kinds=["merge"], apply_merges=True)
    assert stats.considered == 1
    assert stats.applied == 1
    assert stats.failed == 0
    row = nas_conn.execute("SELECT status FROM review_queue WHERE kind = 'merge'").fetchone()
    assert row["status"] == "applied"


def test_apply_reviewed_circuit_breaker_stops_early(respx_mock, client, nas_conn):
    _bootstrap(respx_mock)

    def _dispatch(request):
        method = _form(request).get("method")
        if method == "list_face":
            return httpx.Response(200, json=LIST_FACE_RESP_EMPTY)
        if method == "add_face":
            return httpx.Response(200, json={"success": False, "error": {"code": 999}})
        raise AssertionError(f"unexpected call: method={method!r}")

    respx_mock.post(f"{NAS_BASE_URL}/webapi/entry.cgi").mock(side_effect=_dispatch)

    writer = writeback.SynoWriter(client, nas_conn, "personal")
    for i in range(7):
        _insert_review_row(
            nas_conn,
            "assign",
            {
                "face_id": i, "photo_id": 1000 + i, "space": "personal", "person_id": 5000 + i,
                "person_name": None, "bbox_normalized": [0.1, 0.1, 0.2, 0.2], "confidence": 0.9,
            },
        )

    total_approved = nas_conn.execute(
        "SELECT COUNT(*) c FROM review_queue WHERE status = 'approved'"
    ).fetchone()["c"]
    assert total_approved == 7

    stats = writeback.apply_reviewed(nas_conn, writer, kinds=["assign"], stop_after_failures=5)
    assert stats.considered < total_approved
    assert stats.failed == 5
    remaining_approved = nas_conn.execute(
        "SELECT COUNT(*) c FROM review_queue WHERE status = 'approved'"
    ).fetchone()["c"]
    assert remaining_approved == 2


def test_apply_reviewed_merge_gated_by_apply_merges(respx_mock, client, nas_conn):
    _bootstrap(respx_mock)
    # No entry.cgi route registered at all -- any merge/get call would error.
    writer = writeback.SynoWriter(client, nas_conn, "personal")
    _insert_review_row(
        nas_conn,
        "merge",
        {
            "person_a": {"space": "personal", "person_id": 10, "name": "A"},
            "person_b": {"space": "personal", "person_id": 20, "name": "B"},
            "evidence": {},
        },
    )
    stats = writeback.apply_reviewed(nas_conn, writer, kinds=["merge"], apply_merges=False)
    assert stats.considered == 1
    assert stats.skipped == 1
    row = nas_conn.execute("SELECT status FROM review_queue WHERE kind = 'merge'").fetchone()
    assert row["status"] == "approved"


def test_apply_reviewed_merge_applies_when_enabled(respx_mock, client, nas_conn):
    _bootstrap(respx_mock)

    def _dispatch(request):
        method = _form(request).get("method")
        if method == "get":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {"list": [{"id": 20, "name": "B", "item_count": 1, "show": True, "cover": 1}]},
                },
            )
        if method == "merge":
            return httpx.Response(200, json={"success": True})
        raise AssertionError(f"unexpected call: method={method!r}")

    respx_mock.post(f"{NAS_BASE_URL}/webapi/entry.cgi").mock(side_effect=_dispatch)

    writer = writeback.SynoWriter(client, nas_conn, "personal")
    _insert_review_row(
        nas_conn,
        "merge",
        {
            "person_a": {"space": "personal", "person_id": 10, "name": "A"},
            "person_b": {"space": "personal", "person_id": 20, "name": "B"},
            "evidence": {},
        },
    )
    stats = writeback.apply_reviewed(nas_conn, writer, kinds=["merge"], apply_merges=True)
    assert stats.applied == 1
    row = nas_conn.execute("SELECT status FROM review_queue WHERE kind = 'merge'").fetchone()
    assert row["status"] == "applied"


def test_apply_reviewed_dry_run_does_not_consume_approvals(respx_mock, nas_conn):
    # A dry run must rehearse stats but leave review_queue.status untouched —
    # otherwise the preview consumes the approvals a real --apply run needs.
    writer = writeback.DryRunWriter(nas_conn)
    _insert_review_row(
        nas_conn,
        "assign",
        {
            "face_id": 1, "photo_id": 103153, "space": "personal", "person_id": 2660,
            "person_name": None, "bbox_normalized": [0.1, 0.1, 0.2, 0.2], "confidence": 0.9,
        },
    )

    stats = writeback.apply_reviewed(nas_conn, writer, kinds=["assign"], apply_merges=False)

    assert stats.considered == 1
    assert stats.applied == 1
    assert len(respx_mock.calls) == 0
    row = nas_conn.execute("SELECT status FROM review_queue WHERE kind = 'assign'").fetchone()
    assert row["status"] == "approved"
