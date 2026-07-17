"""Duplicate detection (dedupe.py) and deletion path (dedupe_writeback.py)."""

from __future__ import annotations

import urllib.parse

import httpx
import pytest

from synopticon import audit, dedupe
from synopticon.db import store
from synopticon.dedupe_writeback import delete_items
from synopticon.sync.hashes import phash_hamming
from synopticon.syno.client import SynoClient
from tests.unit.conftest import NAS_BASE_URL

API_INFO = {
    "success": True,
    "data": {
        "SYNO.API.Auth": {"minVersion": 1, "maxVersion": 6, "path": "auth.cgi"},
        "SYNO.Foto.Browse.Item": {"minVersion": 1, "maxVersion": 7, "path": "entry.cgi"},
        "SYNO.Foto.BackgroundTask.File": {"minVersion": 1, "maxVersion": 1, "path": "entry.cgi"},
    },
}
LOGIN_OK = {"success": True, "data": {"sid": "sid-1", "synotoken": "tok-1", "did": "did-1"}}
# Shape mirrored from har/deleting_multiple_photos.har.
DELETE_RESP = {
    "success": True,
    "data": {"task_info": {"id": 9, "operation": "delete", "status": "waiting", "total": 2}},
}


def _insert_photo(conn, space, pid, *, sha256=None, phash=None, w=1000, h=1000,
                  filesize=1000, filename=None, deleted=0):
    conn.execute(
        "INSERT INTO photos (id, space, filename, filesize, type, width, height, "
        "sha256, phash, deleted, synced_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (pid, space, filename or f"IMG_{pid}.jpg", filesize, "photo", w, h,
         sha256, phash, deleted, store.now()),
    )
    conn.commit()


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


# -- phash_hamming ------------------------------------------------------------


def test_phash_hamming_counts_differing_bits():
    assert phash_hamming("0000000000000000", "0000000000000000") == 0
    assert phash_hamming("0000000000000000", "0000000000000001") == 1
    assert phash_hamming("0000000000000000", "000000000000000f") == 4
    assert phash_hamming("0000000000000000", "ffffffffffffffff") == 64


# -- _pick_keep ---------------------------------------------------------------


def test_pick_keep_prefers_resolution_then_filesize_then_id(nas_conn):
    _insert_photo(nas_conn, "personal", 1, w=1000, h=1000, filesize=500)
    _insert_photo(nas_conn, "personal", 2, w=2000, h=2000, filesize=100)  # highest res
    _insert_photo(nas_conn, "personal", 3, w=2000, h=2000, filesize=999)  # same res, bigger
    rows = nas_conn.execute("SELECT * FROM photos ORDER BY id").fetchall()
    keep, drop = dedupe._pick_keep(rows)
    assert keep["id"] == 3  # 2000x2000 and largest file wins
    assert {r["id"] for r in drop} == {1, 2}


def test_pick_keep_ties_break_on_lowest_id(nas_conn):
    # Byte-identical duplicates: equal resolution + filesize -> lowest id kept.
    _insert_photo(nas_conn, "personal", 7, w=800, h=600, filesize=42)
    _insert_photo(nas_conn, "personal", 3, w=800, h=600, filesize=42)
    rows = nas_conn.execute("SELECT * FROM photos").fetchall()
    keep, drop = dedupe._pick_keep(rows)
    assert keep["id"] == 3
    assert [r["id"] for r in drop] == [7]


# -- find_exact ---------------------------------------------------------------


def test_find_exact_groups_by_sha256_and_excludes_singletons(nas_conn):
    _insert_photo(nas_conn, "personal", 1, sha256="aaa", w=1000, h=1000)
    _insert_photo(nas_conn, "personal", 2, sha256="aaa", w=2000, h=2000)  # kept (higher res)
    _insert_photo(nas_conn, "personal", 3, sha256="bbb")  # singleton -> ignored
    _insert_photo(nas_conn, "personal", 4, sha256=None)   # unhashed -> ignored
    groups = dedupe.find_exact(nas_conn, "personal")
    assert len(groups) == 1
    (g,) = groups
    assert g.kind == "exact" and g.keep["id"] == 2
    assert [r["id"] for r in g.drop] == [1]
    assert g.reclaimable_bytes == 1000


def test_find_exact_ignores_deleted_rows(nas_conn):
    _insert_photo(nas_conn, "personal", 1, sha256="aaa")
    _insert_photo(nas_conn, "personal", 2, sha256="aaa", deleted=1)
    assert dedupe.find_exact(nas_conn, "personal") == []


def test_find_exact_scopes_to_space(nas_conn):
    _insert_photo(nas_conn, "personal", 1, sha256="aaa")
    _insert_photo(nas_conn, "shared", 2, sha256="aaa")
    assert dedupe.find_exact(nas_conn, "personal") == []


# -- find_visual --------------------------------------------------------------


def test_find_visual_groups_within_threshold(nas_conn):
    # 0x0..0 and 0x0..1 differ by 1 bit; 0xffff.. differs by 64.
    _insert_photo(nas_conn, "personal", 1, phash="0000000000000000", w=1000, h=1000)
    _insert_photo(nas_conn, "personal", 2, phash="0000000000000001", w=2000, h=2000)
    _insert_photo(nas_conn, "personal", 3, phash="ffffffffffffffff")
    groups = dedupe.find_visual(nas_conn, "personal", threshold=5)
    assert len(groups) == 1
    (g,) = groups
    assert g.kind == "visual" and g.keep["id"] == 2
    assert [r["id"] for r in g.drop] == [1]


def test_find_visual_splits_beyond_threshold(nas_conn):
    _insert_photo(nas_conn, "personal", 1, phash="0000000000000000")
    _insert_photo(nas_conn, "personal", 2, phash="000000000000000f")  # 4 bits away
    assert dedupe.find_visual(nas_conn, "personal", threshold=2) == []
    assert len(dedupe.find_visual(nas_conn, "personal", threshold=4)) == 1


def test_find_visual_chains_transitively(nas_conn):
    # 1-2 within threshold, 2-3 within threshold, 1-3 beyond: union-find still
    # merges all three into one component.
    _insert_photo(nas_conn, "personal", 1, phash="0000000000000000")
    _insert_photo(nas_conn, "personal", 2, phash="0000000000000003")  # 2 bits from 1
    _insert_photo(nas_conn, "personal", 3, phash="000000000000000f")  # 2 bits from 2, 4 from 1
    groups = dedupe.find_visual(nas_conn, "personal", threshold=2)
    assert len(groups) == 1
    (g,) = groups
    assert len(g.drop) == 2  # one kept, two dropped


# -- collect_drop_ids ---------------------------------------------------------


def test_collect_drop_ids_dedups_across_levels(nas_conn):
    # Same photo qualifies as an exact and a visual drop; deleted only once.
    _insert_photo(nas_conn, "personal", 1, sha256="aaa", phash="0000000000000000", w=2000, h=2000)
    _insert_photo(nas_conn, "personal", 2, sha256="aaa", phash="0000000000000000", w=1000, h=1000)
    groups = dedupe.find_exact(nas_conn, "personal") + dedupe.find_visual(nas_conn, "personal", 5)
    assert len(groups) == 2  # one exact, one visual
    assert dedupe.collect_drop_ids(groups) == [2]  # id 2 only, once


# -- delete_items: dry run ----------------------------------------------------


def test_delete_items_dry_run_audits_without_network(respx_mock, nas_conn):
    _insert_photo(nas_conn, "personal", 1)
    result = delete_items(nas_conn, None, "personal", [1], dry_run=True)
    assert result == {"deleted": 0, "skipped": 0, "failed": 0}
    assert len(respx_mock.calls) == 0
    assert nas_conn.execute("SELECT deleted FROM photos WHERE id=1").fetchone()[0] == 0
    rows = audit.tail(nas_conn, limit=5)
    assert any(r["action"] == "dryrun.delete" for r in rows)


# -- delete_items: real deletion ----------------------------------------------


def test_delete_items_deletes_live_items_and_marks_local(respx_mock, client, nas_conn):
    _bootstrap(respx_mock)
    _insert_photo(nas_conn, "personal", 101)
    _insert_photo(nas_conn, "personal", 102)

    def _dispatch(request):
        method = _form(request).get("method")
        if method == "get":  # idempotency pre-check -> both live
            return httpx.Response(200, json={"success": True, "data": {"list": [{"id": 1}]}})
        if method == "delete":
            return httpx.Response(200, json=DELETE_RESP)
        raise AssertionError(f"unexpected method {method!r}")

    route = respx_mock.post(f"{NAS_BASE_URL}/webapi/entry.cgi").mock(side_effect=_dispatch)

    result = delete_items(nas_conn, client, "personal", [101, 102], dry_run=False)
    assert result == {"deleted": 2, "skipped": 0, "failed": 0}

    # Both marked deleted locally.
    deleted = nas_conn.execute("SELECT id FROM photos WHERE deleted=1 ORDER BY id").fetchall()
    assert [r["id"] for r in deleted] == [101, 102]

    # The delete call carried a batched item_id array and empty folder_id.
    delete_call = next(
        c for c in route.calls if _form(c.request).get("method") == "delete"
    )
    form = _form(delete_call.request)
    assert form["item_id"] == "[101,102]"
    assert form["folder_id"] == "[]"
    assert form["version"] == "1"

    rows = audit.tail(nas_conn, limit=5)
    assert any(r["action"] == "dedupe.delete" and r["success"] == 1 for r in rows)


def test_delete_items_skips_already_gone(respx_mock, client, nas_conn):
    _bootstrap(respx_mock)
    _insert_photo(nas_conn, "personal", 200)

    def _dispatch(request):
        method = _form(request).get("method")
        if method == "get":  # empty list -> get_item raises LookupError
            return httpx.Response(200, json={"success": True, "data": {"list": []}})
        raise AssertionError("delete should not be called for a gone item")

    respx_mock.post(f"{NAS_BASE_URL}/webapi/entry.cgi").mock(side_effect=_dispatch)

    result = delete_items(nas_conn, client, "personal", [200], dry_run=False)
    assert result == {"deleted": 0, "skipped": 1, "failed": 0}
    assert nas_conn.execute("SELECT deleted FROM photos WHERE id=200").fetchone()[0] == 0


def test_delete_items_records_failure(respx_mock, client, nas_conn):
    _bootstrap(respx_mock)
    _insert_photo(nas_conn, "personal", 300)

    def _dispatch(request):
        method = _form(request).get("method")
        if method == "get":
            return httpx.Response(200, json={"success": True, "data": {"list": [{"id": 1}]}})
        if method == "delete":
            return httpx.Response(200, json={"success": False, "error": {"code": 642}})
        raise AssertionError(f"unexpected method {method!r}")

    respx_mock.post(f"{NAS_BASE_URL}/webapi/entry.cgi").mock(side_effect=_dispatch)

    result = delete_items(nas_conn, client, "personal", [300], dry_run=False)
    assert result == {"deleted": 0, "skipped": 0, "failed": 1}
    # Not marked deleted; failure audited.
    assert nas_conn.execute("SELECT deleted FROM photos WHERE id=300").fetchone()[0] == 0
    rows = audit.tail(nas_conn, limit=5)
    assert any(r["action"] == "dedupe.delete" and r["success"] == 0 for r in rows)
