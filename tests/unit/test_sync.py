"""sync.items / sync.persons: paginated upsert passes against a mocked SynoClient."""

from __future__ import annotations

import urllib.parse

import httpx
import pytest

from synopticon.db import store
from synopticon.sync import items as sync_items_mod
from synopticon.sync import persons as sync_persons_mod
from synopticon.syno.client import SynoClient
from tests.unit.conftest import NAS_BASE_URL

API_INFO = {
    "success": True,
    "data": {
        "SYNO.API.Auth": {"minVersion": 1, "maxVersion": 6, "path": "auth.cgi"},
        "SYNO.Foto.Browse.Item": {"minVersion": 1, "maxVersion": 7, "path": "entry.cgi"},
        "SYNO.Foto.Browse.Person": {"minVersion": 1, "maxVersion": 3, "path": "entry.cgi"},
    },
}
LOGIN_OK = {"success": True, "data": {"sid": "sid-1", "synotoken": "tok-1", "did": "did-1"}}


def _form(request: httpx.Request) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(request.content.decode()))


def _bootstrap(respx_mock):
    respx_mock.post(f"{NAS_BASE_URL}/webapi/query.cgi").mock(
        return_value=httpx.Response(200, json=API_INFO)
    )
    respx_mock.post(f"{NAS_BASE_URL}/webapi/auth.cgi").mock(
        return_value=httpx.Response(200, json=LOGIN_OK)
    )


def _item(id_: int, person_ids: list[int]) -> dict:
    return {
        "id": id_,
        "filename": f"photo-{id_}.jpg",
        "filesize": 1000 + id_,
        "folder_id": 1,
        "time": 1700000000 + id_,
        "indexed_time": 1700000000000 + id_,
        "type": "photo",
        "additional": {
            "thumbnail": {"cache_key": f"{id_}_ck", "unit_id": id_},
            "resolution": {"width": 100, "height": 200},
            "orientation": 1,
            "person": [{"id": pid} for pid in person_ids],
        },
    }


def _page(items: list[dict]) -> httpx.Response:
    return httpx.Response(200, json={"success": True, "data": {"list": items}})


@pytest.fixture
def client(nas_settings, nas_conn):
    c = SynoClient(nas_settings, nas_conn)
    yield c
    c.close()


def test_sync_items_idempotent_and_deletion_lifecycle(respx_mock, client, nas_conn):
    _bootstrap(respx_mock)
    route = respx_mock.post(f"{NAS_BASE_URL}/webapi/entry.cgi")

    # Pass 1: photo 1 -> person 10, photo 2 -> person 20.
    route.mock(return_value=_page([_item(1, [10]), _item(2, [20])]))
    stats1 = sync_items_mod.sync_items(nas_conn, client, "personal")
    assert stats1 == {"seen": 2, "upserted": 2, "deleted": 0}
    rows = nas_conn.execute(
        "SELECT id, deleted FROM photos WHERE space = 'personal' ORDER BY id"
    ).fetchall()
    assert [(r["id"], r["deleted"]) for r in rows] == [(1, 0), (2, 0)]

    # Re-run with an identical page: idempotent, no duplicate rows.
    stats1b = sync_items_mod.sync_items(nas_conn, client, "personal")
    assert stats1b == {"seen": 2, "upserted": 2, "deleted": 0}
    count = nas_conn.execute(
        "SELECT COUNT(*) c FROM photos WHERE space = 'personal'"
    ).fetchone()["c"]
    assert count == 2

    # Pass 2: photo 2 disappears; photo 1's person changes 10 -> 20.
    route.mock(return_value=_page([_item(1, [20])]))
    stats2 = sync_items_mod.sync_items(nas_conn, client, "personal")
    assert stats2 == {"seen": 1, "upserted": 1, "deleted": 1}
    deleted_row = nas_conn.execute(
        "SELECT deleted FROM photos WHERE space = 'personal' AND id = 2"
    ).fetchone()
    assert deleted_row["deleted"] == 1
    pp = nas_conn.execute(
        "SELECT person_id FROM person_photos WHERE space = 'personal' AND photo_id = 1"
    ).fetchall()
    assert [r["person_id"] for r in pp] == [20]

    # Pass 3: photo 2 reappears -> un-deleted.
    route.mock(return_value=_page([_item(1, [20]), _item(2, [20])]))
    stats3 = sync_items_mod.sync_items(nas_conn, client, "personal")
    assert stats3 == {"seen": 2, "upserted": 2, "deleted": 0}
    reappeared = nas_conn.execute(
        "SELECT deleted FROM photos WHERE space = 'personal' AND id = 2"
    ).fetchone()
    assert reappeared["deleted"] == 0


def test_sync_persons_basic_upsert_and_deletion(respx_mock, client, nas_conn):
    _bootstrap(respx_mock)
    route = respx_mock.post(f"{NAS_BASE_URL}/webapi/entry.cgi")

    route.mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "list": [
                        {"id": 10, "name": "Alice", "item_count": 5, "show": True, "cover": 1},
                        {"id": 20, "name": "Bob", "item_count": 3, "show": True, "cover": 2},
                    ]
                },
            },
        )
    )
    stats1 = sync_persons_mod.sync_persons(nas_conn, client, "personal")
    assert stats1 == {"seen": 2, "upserted": 2, "deleted": 0}

    route.mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "data": {"list": [{"id": 10, "name": "Alice", "item_count": 5, "show": True, "cover": 1}]},
            },
        )
    )
    stats2 = sync_persons_mod.sync_persons(nas_conn, client, "personal")
    assert stats2 == {"seen": 1, "upserted": 1, "deleted": 1}
    bob = nas_conn.execute(
        "SELECT deleted FROM persons WHERE space = 'personal' AND id = 20"
    ).fetchone()
    assert bob["deleted"] == 1


def test_sync_faces_upserts_and_resumes(respx_mock, client, nas_conn):
    _bootstrap(respx_mock)
    now = store.now()
    for pid in (1, 2, 3):
        nas_conn.execute(
            "INSERT INTO photos (id, space, synced_at) VALUES (?, 'personal', ?)", (pid, now)
        )
        nas_conn.execute(
            "INSERT INTO person_photos (space, person_id, photo_id, synced_at) "
            "VALUES ('personal', 100, ?, ?)",
            (pid, now),
        )
    nas_conn.commit()

    calls: list[int] = []

    def _handler(request):
        body = _form(request)
        id_item = int(body["id_item"])
        calls.append(id_item)
        face = {
            "face_id": 1000 + id_item,
            "person_id": 100,
            "name": "Someone",
            "face_bounding_box": {
                "top_left": {"x": 0.1, "y": 0.1},
                "bottom_right": {"x": 0.2, "y": 0.2},
            },
        }
        return httpx.Response(200, json={"success": True, "data": {"list": [face]}})

    respx_mock.post(f"{NAS_BASE_URL}/webapi/entry.cgi").mock(side_effect=_handler)

    stats = sync_persons_mod.sync_faces(nas_conn, client, "personal")
    assert stats == {"photos_processed": 3, "faces_upserted": 3, "faces_skipped": 0}
    assert calls == [1, 2, 3]

    rows = nas_conn.execute(
        "SELECT syno_face_id, photo_id FROM syno_faces WHERE space = 'personal' ORDER BY photo_id"
    ).fetchall()
    assert [(r["syno_face_id"], r["photo_id"]) for r in rows] == [(1001, 1), (1002, 2), (1003, 3)]
    assert store.get_state(nas_conn, "sync_faces_cursor_personal") is None

    # Resumability: leave a cursor mid-way, confirm already-processed ids are skipped
    # and the cursor is cleared again once the (remaining) pass completes.
    calls.clear()
    store.set_state(nas_conn, "sync_faces_cursor_personal", 2)
    stats2 = sync_persons_mod.sync_faces(nas_conn, client, "personal", resume=True)
    assert calls == [3]
    assert stats2 == {"photos_processed": 1, "faces_upserted": 1, "faces_skipped": 0}
    assert store.get_state(nas_conn, "sync_faces_cursor_personal") is None


def test_sync_faces_skips_unlistable_photo(respx_mock, client, nas_conn):
    """A per-photo API error (e.g. code 117) is skipped, cursor advances, sweep continues."""
    _bootstrap(respx_mock)
    now = store.now()
    for pid in (1, 2, 3):
        nas_conn.execute(
            "INSERT INTO photos (id, space, synced_at) VALUES (?, 'personal', ?)", (pid, now)
        )
        nas_conn.execute(
            "INSERT INTO person_photos (space, person_id, photo_id, synced_at) "
            "VALUES ('personal', 100, ?, ?)",
            (pid, now),
        )
    nas_conn.commit()

    calls: list[int] = []

    def _handler(request):
        id_item = int(_form(request)["id_item"])
        calls.append(id_item)
        if id_item == 2:  # the poison photo
            return httpx.Response(200, json={"success": False, "error": {"code": 117}})
        face = {
            "face_id": 1000 + id_item,
            "person_id": 100,
            "name": "Someone",
            "face_bounding_box": {
                "top_left": {"x": 0.1, "y": 0.1},
                "bottom_right": {"x": 0.2, "y": 0.2},
            },
        }
        return httpx.Response(200, json={"success": True, "data": {"list": [face]}})

    respx_mock.post(f"{NAS_BASE_URL}/webapi/entry.cgi").mock(side_effect=_handler)

    skips: list[tuple] = []
    stats = sync_persons_mod.sync_faces(
        nas_conn, client, "personal", on_skip=lambda pid, code, url: skips.append((pid, code, url))
    )

    # Photo 2 failed but 1 and 3 still processed; the sweep did not abort.
    assert calls == [1, 2, 3]
    assert stats == {"photos_processed": 2, "faces_upserted": 2, "faces_skipped": 1}
    assert [(s[0], s[1]) for s in skips] == [(2, 117)]
    assert skips[0][2] and skips[0][2].endswith("/personal_space/timeline/item/2")

    faces = nas_conn.execute(
        "SELECT photo_id FROM syno_faces WHERE space = 'personal' ORDER BY photo_id"
    ).fetchall()
    assert [r["photo_id"] for r in faces] == [1, 3]
    # Full pass completed -> cursor cleared, so the skipped photo is retried next pass.
    assert store.get_state(nas_conn, "sync_faces_cursor_personal") is None


def test_sync_faces_excludes_tags_for_deleted_photos(respx_mock, client, nas_conn):
    """A tag left dangling on a deleted photo is never queried (root cause of the 117)."""
    _bootstrap(respx_mock)
    now = store.now()
    # Photo 1 live, photo 2 deleted on the NAS but still tagged in person_photos.
    nas_conn.execute("INSERT INTO photos (id, space, synced_at, deleted) VALUES (1,'personal',?,0)", (now,))
    nas_conn.execute("INSERT INTO photos (id, space, synced_at, deleted) VALUES (2,'personal',?,1)", (now,))
    for pid in (1, 2):
        nas_conn.execute(
            "INSERT INTO person_photos (space, person_id, photo_id, synced_at) "
            "VALUES ('personal', 100, ?, ?)",
            (pid, now),
        )
    nas_conn.commit()

    calls: list[int] = []

    def _handler(request):
        id_item = int(_form(request)["id_item"])
        calls.append(id_item)
        return httpx.Response(200, json={"success": True, "data": {"list": []}})

    respx_mock.post(f"{NAS_BASE_URL}/webapi/entry.cgi").mock(side_effect=_handler)

    stats = sync_persons_mod.sync_faces(nas_conn, client, "personal")
    # The deleted photo is never listed; no skip needed because it was excluded up front.
    assert calls == [1]
    assert stats == {"photos_processed": 1, "faces_upserted": 0, "faces_skipped": 0}
