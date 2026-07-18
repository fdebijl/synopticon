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

# Verbatim from har/reassign_existing_face_to_other_person.har: entry 10's
# response (Person.separate), and entries 0/13 (list_face before/after the move
# — same face_id, re-bound person).
SEPARATE_RESP = {
    "data": {
        "additional": {"thumbnail": {"cache_key": "64599_1691070797"}},
        "cover": 47412,
        "id": 7185,
        "item_count": 256,
        "name": "Hannah Lips",
        "show": True,
    },
    "success": True,
}
_REASSIGN_BBOX = {
    "top_left": {"x": 0.453206817586503, "y": 0.21220010263430777},
    "bottom_right": {"x": 0.70277045712515362, "y": 0.39933720102046893},
}
LIST_FACE_RESP_WRONG_PERSON = {
    "data": {
        "list": [
            {
                "face_bounding_box": _REASSIGN_BBOX,
                "face_id": 79940,
                "name": "Hannah Todd",
                "person_id": 15315,
                "thumbnail": {"cache_key": "84364_1739711312"},
            }
        ]
    },
    "success": True,
}
LIST_FACE_RESP_REBOUND = {
    "data": {
        "list": [
            {
                "face_bounding_box": _REASSIGN_BBOX,
                "face_id": 79940,
                "name": "Hannah Lips",
                "person_id": 7185,
                "thumbnail": {"cache_key": "64599_1691070797"},
            }
        ]
    },
    "success": True,
}

REASSIGN_PAYLOAD = {
    "face_id": 1,
    "photo_id": 103181,
    "space": "personal",
    "syno_face_id": 79940,
    "from_person_id": 15315,
    "from_person_name": "Hannah Todd",
    "person_id": 7185,
    "person_name": "Hannah Lips",
    "bbox_normalized": [0.4532, 0.2122, 0.7028, 0.3993],
    "confidence": 0.91,
    "from_similarity": 0.30,
}


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
    r4 = writer.delete_face("personal", 5, 10)
    r5 = writer.reassign("personal", 79940, 7185, "Hannah Lips", 103181)

    assert r1.success and r2.success and r3.success and r4.success and r5.success
    assert len(respx_mock.calls) == 0

    rows = audit.tail(nas_conn, limit=10)
    dryrun_rows = [r for r in rows if r["action"].startswith("dryrun.")]
    assert {r["action"] for r in dryrun_rows} == {
        "dryrun.assign", "dryrun.merge", "dryrun.rename", "dryrun.delete_face",
        "dryrun.reassign",
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


def test_apply_reviewed_merge_named_not_applied_by_apply_merges(respx_mock, client, nas_conn):
    # A named->named merge must NOT ride in on the ordinary --apply-merges gate;
    # it needs its own apply_merges_named. No entry.cgi route is registered, so
    # any write/probe would raise.
    _bootstrap(respx_mock)
    writer = writeback.SynoWriter(client, nas_conn, "personal")
    _insert_review_row(
        nas_conn,
        "merge_named",
        {
            "person_a": {"space": "personal", "person_id": 10, "name": "Alice"},
            "person_b": {"space": "personal", "person_id": 20, "name": "Bob"},
            "evidence": {},
        },
    )
    stats = writeback.apply_reviewed(
        nas_conn, writer, kinds=["merge_named"], apply_merges=True, apply_merges_named=False
    )
    assert stats.considered == 1
    assert stats.skipped == 1
    row = nas_conn.execute(
        "SELECT status FROM review_queue WHERE kind = 'merge_named'"
    ).fetchone()
    assert row["status"] == "approved"


def test_apply_reviewed_merge_named_applies_when_gate_open(respx_mock, client, nas_conn):
    _bootstrap(respx_mock)

    def _dispatch(request):
        method = _form(request).get("method")
        if method == "get":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {"list": [{"id": 20, "name": "Bob", "item_count": 3, "show": True, "cover": 1}]},
                },
            )
        if method == "merge":
            return httpx.Response(200, json={"success": True})
        raise AssertionError(f"unexpected call: method={method!r}")

    respx_mock.post(f"{NAS_BASE_URL}/webapi/entry.cgi").mock(side_effect=_dispatch)

    writer = writeback.SynoWriter(client, nas_conn, "personal")
    _insert_review_row(
        nas_conn,
        "merge_named",
        {
            "person_a": {"space": "personal", "person_id": 10, "name": "Alice"},
            "person_b": {"space": "personal", "person_id": 20, "name": "Bob"},
            "evidence": {},
        },
    )
    stats = writeback.apply_reviewed(
        nas_conn, writer, kinds=["merge_named"], apply_merges_named=True
    )
    assert stats.applied == 1
    row = nas_conn.execute(
        "SELECT status FROM review_queue WHERE kind = 'merge_named'"
    ).fetchone()
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


def test_apply_reviewed_applies_low_confidence_as_assign(respx_mock, client, nas_conn):
    # low_confidence rows carry the same photo_id/person_id/bbox payload as
    # assigns and must be written via writer.assign — otherwise reviewer-approved
    # crossref suggestions silently never reach the NAS.
    _bootstrap(respx_mock)
    added = {"done": False}

    def _dispatch(request):
        form = _form(request)
        method = form.get("method")
        if method == "list_face":
            # Absent before add_face (idempotency probe), present after (verify).
            return httpx.Response(
                200, json=LIST_FACE_RESP_TAGGED if added["done"] else LIST_FACE_RESP_EMPTY
            )
        if method == "add_face":
            added["done"] = True
            return httpx.Response(200, json=ADD_FACE_RESP)
        raise AssertionError(f"unexpected call: method={method!r}")

    respx_mock.post(f"{NAS_BASE_URL}/webapi/entry.cgi").mock(side_effect=_dispatch)

    writer = writeback.SynoWriter(client, nas_conn, "personal")
    _insert_review_row(
        nas_conn,
        "low_confidence",
        {
            "face_id": 1882, "photo_id": 103153, "space": "personal", "person_id": 2660,
            "person_name": "Tim Jansen", "bbox_normalized": [0.1, 0.1, 0.2, 0.2], "confidence": 0.51,
        },
    )

    stats = writeback.apply_reviewed(nas_conn, writer, kinds=["low_confidence"], apply_merges=False)

    assert stats.considered == 1
    assert stats.applied == 1
    assert stats.skipped == 0
    assert added["done"] is True  # add_face actually fired
    row = nas_conn.execute("SELECT status FROM review_queue WHERE kind = 'low_confidence'").fetchone()
    assert row["status"] == "applied"


# -- apply_reviewed: reassign ---------------------------------------------------


def test_apply_reviewed_reassign_gated_without_flag(respx_mock, client, nas_conn):
    _bootstrap(respx_mock)
    # No entry.cgi route registered -- the gate must trip before any NAS probe.
    writer = writeback.SynoWriter(client, nas_conn, "personal")
    _insert_review_row(nas_conn, "reassign", REASSIGN_PAYLOAD)

    stats = writeback.apply_reviewed(nas_conn, writer, kinds=["reassign"], apply_reassigns=False)
    assert stats.considered == 1
    assert stats.skipped == 1
    row = nas_conn.execute("SELECT status FROM review_queue WHERE kind = 'reassign'").fetchone()
    assert row["status"] == "approved"


def test_apply_reviewed_reassign_full_chain(respx_mock, client, nas_conn):
    _bootstrap(respx_mock)
    separated = {"done": False, "form": None}

    def _dispatch(request):
        form = _form(request)
        method = form.get("method")
        if method == "list_face":
            # Wrong person before separate (idempotency probe), re-bound after (verify).
            return httpx.Response(
                200,
                json=LIST_FACE_RESP_REBOUND if separated["done"] else LIST_FACE_RESP_WRONG_PERSON,
            )
        if method == "separate":
            separated["done"] = True
            separated["form"] = form
            return httpx.Response(200, json=SEPARATE_RESP)
        raise AssertionError(f"unexpected call: method={method!r}")

    respx_mock.post(f"{NAS_BASE_URL}/webapi/entry.cgi").mock(side_effect=_dispatch)

    writer = writeback.SynoWriter(client, nas_conn, "personal")
    _insert_review_row(nas_conn, "reassign", REASSIGN_PAYLOAD)

    stats = writeback.apply_reviewed(nas_conn, writer, kinds=["reassign"], apply_reassigns=True)
    assert stats.considered == 1
    assert stats.applied == 1
    assert stats.failed == 0

    # HAR-exact payload shape: JSON list face_id, plain target_id, quoted name.
    assert separated["form"]["face_id"] == "[79940]"
    assert separated["form"]["target_id"] == "7185"
    assert separated["form"]["name"] == '"Hannah Lips"'

    row = nas_conn.execute("SELECT status FROM review_queue WHERE kind = 'reassign'").fetchone()
    assert row["status"] == "applied"
    rows = audit.tail(nas_conn, limit=10)
    actions = sorted(r["action"] for r in rows if r["action"].startswith("writeback.reassign"))
    assert actions == ["writeback.reassign.separate", "writeback.reassign.verify"]
    assert all(r["success"] == 1 for r in rows if r["action"].startswith("writeback.reassign"))


def test_apply_reviewed_reassign_uses_fresh_person_name(respx_mock, client, nas_conn):
    # The separate call must carry the target's current name from the local
    # mirror, not the possibly-stale name captured in the payload.
    _bootstrap(respx_mock)
    nas_conn.execute(
        "INSERT INTO persons (space, id, name, synced_at) VALUES ('personal', 7185, 'Hannah L.', 0)"
    )
    nas_conn.commit()
    separated = {"done": False, "form": None}

    def _dispatch(request):
        form = _form(request)
        method = form.get("method")
        if method == "list_face":
            return httpx.Response(
                200,
                json=LIST_FACE_RESP_REBOUND if separated["done"] else LIST_FACE_RESP_WRONG_PERSON,
            )
        if method == "separate":
            separated["done"] = True
            separated["form"] = form
            return httpx.Response(200, json=SEPARATE_RESP)
        raise AssertionError(f"unexpected call: method={method!r}")

    respx_mock.post(f"{NAS_BASE_URL}/webapi/entry.cgi").mock(side_effect=_dispatch)

    writer = writeback.SynoWriter(client, nas_conn, "personal")
    _insert_review_row(nas_conn, "reassign", REASSIGN_PAYLOAD)

    stats = writeback.apply_reviewed(nas_conn, writer, kinds=["reassign"], apply_reassigns=True)
    assert stats.applied == 1
    assert separated["form"]["name"] == '"Hannah L."'


def test_apply_reviewed_reassign_idempotent_skip(respx_mock, client, nas_conn):
    _bootstrap(respx_mock)

    def _dispatch(request):
        method = _form(request).get("method")
        if method == "list_face":
            return httpx.Response(200, json=LIST_FACE_RESP_REBOUND)
        raise AssertionError(f"unexpected call: method={method!r} (separate must not fire)")

    respx_mock.post(f"{NAS_BASE_URL}/webapi/entry.cgi").mock(side_effect=_dispatch)

    writer = writeback.SynoWriter(client, nas_conn, "personal")
    _insert_review_row(nas_conn, "reassign", REASSIGN_PAYLOAD)

    stats = writeback.apply_reviewed(nas_conn, writer, kinds=["reassign"], apply_reassigns=True)
    assert stats.applied == 1
    assert stats.failed == 0
    row = nas_conn.execute("SELECT status FROM review_queue WHERE kind = 'reassign'").fetchone()
    assert row["status"] == "applied"


def test_apply_reviewed_reassign_face_gone_skips(respx_mock, client, nas_conn):
    # The Synology face vanished since the last sync (NAS drift): nothing to
    # move, so the row must be skipped without writes and stay approved.
    _bootstrap(respx_mock)

    def _dispatch(request):
        method = _form(request).get("method")
        if method == "list_face":
            return httpx.Response(200, json=LIST_FACE_RESP_EMPTY)
        raise AssertionError(f"unexpected call: method={method!r} (separate must not fire)")

    respx_mock.post(f"{NAS_BASE_URL}/webapi/entry.cgi").mock(side_effect=_dispatch)

    writer = writeback.SynoWriter(client, nas_conn, "personal")
    _insert_review_row(nas_conn, "reassign", REASSIGN_PAYLOAD)

    stats = writeback.apply_reviewed(nas_conn, writer, kinds=["reassign"], apply_reassigns=True)
    assert stats.considered == 1
    assert stats.skipped == 1
    assert stats.applied == 0
    row = nas_conn.execute("SELECT status FROM review_queue WHERE kind = 'reassign'").fetchone()
    assert row["status"] == "approved"


def test_apply_reviewed_reassign_separate_failure_marks_failed(respx_mock, client, nas_conn):
    _bootstrap(respx_mock)

    def _dispatch(request):
        method = _form(request).get("method")
        if method == "list_face":
            return httpx.Response(200, json=LIST_FACE_RESP_WRONG_PERSON)
        if method == "separate":
            return httpx.Response(200, json={"success": False, "error": {"code": 999}})
        raise AssertionError(f"unexpected call: method={method!r}")

    respx_mock.post(f"{NAS_BASE_URL}/webapi/entry.cgi").mock(side_effect=_dispatch)

    writer = writeback.SynoWriter(client, nas_conn, "personal")
    _insert_review_row(nas_conn, "reassign", REASSIGN_PAYLOAD)

    stats = writeback.apply_reviewed(nas_conn, writer, kinds=["reassign"], apply_reassigns=True)
    assert stats.failed == 1
    row = nas_conn.execute("SELECT status FROM review_queue WHERE kind = 'reassign'").fetchone()
    assert row["status"] == "failed"


def test_apply_reviewed_reassign_dry_run_parity(respx_mock, nas_conn):
    writer = writeback.DryRunWriter(nas_conn)
    _insert_review_row(nas_conn, "reassign", REASSIGN_PAYLOAD)

    stats = writeback.apply_reviewed(nas_conn, writer, kinds=["reassign"], apply_reassigns=True)

    assert stats.considered == 1
    assert stats.applied == 1
    assert len(respx_mock.calls) == 0
    row = nas_conn.execute("SELECT status FROM review_queue WHERE kind = 'reassign'").fetchone()
    assert row["status"] == "approved"  # dry run must not consume the approval
    rows = audit.tail(nas_conn, limit=5)
    assert any(r["action"] == "dryrun.reassign" for r in rows)


def test_apply_reviewed_reassign_person_filter(respx_mock, nas_conn):
    # person_id must match either side of the move; dry-run keeps the row
    # approved so the same row can be probed with different filters.
    writer = writeback.DryRunWriter(nas_conn)
    _insert_review_row(nas_conn, "reassign", REASSIGN_PAYLOAD)

    for pid, expected in [(7185, 1), (15315, 1), (99999, 0)]:
        stats = writeback.apply_reviewed(
            nas_conn, writer, kinds=["reassign"], person_id=pid, apply_reassigns=True
        )
        assert stats.considered == expected, f"person_id={pid}"


def test_configure_apply_logging_is_idempotent(tmp_path):
    before = list(writeback.log.handlers)
    logfile = tmp_path / "apply.log"
    try:
        first = writeback.configure_apply_logging(logfile)
        handlers_after_first = list(writeback.log.handlers)
        second = writeback.configure_apply_logging(logfile)

        assert first == second == logfile.resolve()
        # Same path must not stack a second handler.
        assert writeback.log.handlers == handlers_after_first

        writeback.log.info("hello from apply")
        for h in writeback.log.handlers:
            h.flush()
        assert "hello from apply" in logfile.read_text()
    finally:
        for h in writeback.log.handlers:
            if h not in before:
                writeback.log.removeHandler(h)
                h.close()
