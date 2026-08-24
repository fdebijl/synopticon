"""Inspect API: the per-photo debug report and its NAS-proxied image.

Hermetic — the NAS is respx-mocked and the report itself is pure DB work. The
geometry assertions are the point of the file: the report normalizes our pixel
boxes and Synology's 0..1 boxes into one coordinate space, so a rotated photo
whose stored resolution disagrees with the frame we detected against must still
come out with boxes inside the frame.
"""

from __future__ import annotations

import json

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
from synopticon.web.inspect_routes import (  # noqa: E402
    display_size,
    frame_candidates,
    resolve_frame,
    rotate_box,
)
from synopticon.web.jobs import JobManager  # noqa: E402

API_INFO = {
    "success": True,
    "data": {
        "SYNO.API.Auth": {"minVersion": 1, "maxVersion": 6, "path": "auth.cgi"},
        "SYNO.Foto.Thumbnail": {"minVersion": 1, "maxVersion": 2, "path": "entry.cgi"},
    },
}
LOGIN_OK = {
    "success": True,
    "data": {"sid": "sid-1", "synotoken": "tok-1", "did": "did-1"},
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


def _photo(db, pid=1, width=1000, height=500, orientation=1, cache_key="ck-1"):
    db.execute(
        "INSERT INTO photos (id, space, filename, filesize, time, type, cache_key, "
        "unit_id, width, height, orientation, synced_at) "
        "VALUES (?,'personal',?,?,?,'photo',?,?,?,?,?,?)",
        (pid, f"IMG_{pid}.jpg", 1234, 1700000000, cache_key, pid, width, height,
         orientation, store.now()),
    )
    db.commit()


def _face(db, photo_id=1, x=100.0, y=50.0, w=80.0, h=80.0, detector="merged", score=0.9):
    cur = db.execute(
        "INSERT INTO faces (space, photo_id, detector, x, y, w, h, det_score, "
        "det_score_secondary, quality, pipeline_version, created_at) "
        "VALUES ('personal',?,?,?,?,?,?,?,?,?, 'pv1', ?)",
        (photo_id, detector, x, y, w, h, score, 0.7, 22.5, store.now()),
    )
    db.commit()
    return int(cur.lastrowid)


def _mock_nas(respx_mock):
    respx_mock.post(f"{NAS_BASE_URL}/webapi/query.cgi").mock(
        return_value=httpx.Response(200, json=API_INFO)
    )
    respx_mock.post(f"{NAS_BASE_URL}/webapi/auth.cgi").mock(
        return_value=httpx.Response(200, json=LOGIN_OK)
    )


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #
def test_report_normalizes_our_boxes_against_the_display_frame(client, db):
    _photo(db)
    face_id = _face(db)

    r = client.get("/api/inspect/personal/1")
    assert r.status_code == 200
    data = r.json()

    assert data["display"] == {
        "width": 1000,
        "height": 500,
        "rotation": 0,
        "rotation_source": "none",
    }
    (face,) = data["faces"]
    assert face["face_id"] == face_id
    assert face["bbox"] == {"x": 100.0, "y": 50.0, "w": 80.0, "h": 80.0}
    assert face["box"] == {"x": 0.1, "y": 0.1, "w": 0.08, "h": 0.16}
    assert face["det_score"] == 0.9
    assert face["det_score_secondary"] == 0.7
    assert face["quality"] == 22.5
    assert face["crop_url"] is None
    assert data["photo"]["filename"] == "IMG_1.jpg"
    assert data["image_url"] == "/api/inspect/image/personal/1"
    assert "personal_space/timeline/item/1" in data["nas_url"]


def test_report_includes_synology_boxes_and_names(client, db):
    _photo(db)
    db.execute(
        "INSERT INTO persons (id, space, name, synced_at) VALUES (7,'personal','Alice',?)",
        (store.now(),),
    )
    db.execute(
        "INSERT INTO syno_faces (space, syno_face_id, photo_id, person_id, name, "
        "x1, y1, x2, y2, synced_at) VALUES ('personal',55,1,7,NULL,0.2,0.3,0.4,0.6,?)",
        (store.now(),),
    )
    db.commit()

    data = client.get("/api/inspect/personal/1").json()
    (syno,) = data["syno_faces"]
    assert syno["syno_face_id"] == 55
    assert syno["name"] == "Alice"
    assert syno["box"]["x"] == pytest.approx(0.2)
    assert syno["box"]["w"] == pytest.approx(0.2)
    assert syno["box"]["h"] == pytest.approx(0.3)
    assert syno["person_url"].endswith("#/person/personal_space/7")


def test_report_carries_cluster_embeddings_extract_and_review_rows(client, db):
    _photo(db)
    face_id = _face(db)
    now = store.now()
    db.execute(
        "INSERT INTO embeddings (face_id, model, variant, dim, vec, model_version, "
        "created_at) VALUES (?,'arcface_r100','orig',512,?, 'v1', ?)",
        (face_id, b"\x00" * 4, now),
    )
    db.execute("INSERT INTO cluster_runs (run_id, params_json, created_at) VALUES (1,'{}',?)", (now,))
    db.execute("INSERT INTO cluster_runs (run_id, params_json, created_at) VALUES (2,'{}',?)", (now,))
    db.execute(
        "INSERT INTO clusters (run_id, cluster_id, size, mapped_person_id, map_space, "
        "vote_fraction, labeled_count) VALUES (2, 9, 12, 7, 'personal', 0.9, 10)"
    )
    db.execute("INSERT INTO cluster_members (run_id, cluster_id, face_id) VALUES (1, 4, ?)", (face_id,))
    db.execute("INSERT INTO cluster_members (run_id, cluster_id, face_id) VALUES (2, 9, ?)", (face_id,))
    db.execute(
        "INSERT INTO persons (id, space, name, synced_at) VALUES (7,'personal','Alice',?)",
        (now,),
    )
    db.execute(
        "INSERT INTO extract_log (space, photo_id, cache_key, pipeline_version, "
        "face_count, processed_at) VALUES ('personal',1,'ck-1','pv1',1,?)",
        (now,),
    )
    db.execute(
        "INSERT INTO review_queue (kind, payload_json, confidence, status, created_at) "
        "VALUES ('assign', ?, 0.81, 'pending', ?)",
        (json.dumps({"space": "personal", "photo_id": 1, "face_id": face_id, "person_id": 7}), now),
    )
    # A row about some other photo must not leak into this report.
    db.execute(
        "INSERT INTO review_queue (kind, payload_json, status, created_at) "
        "VALUES ('assign', ?, 'pending', ?)",
        (json.dumps({"space": "personal", "photo_id": 2, "face_id": 999}), now),
    )
    db.commit()

    data = client.get("/api/inspect/personal/1").json()
    (face,) = data["faces"]
    assert face["embeddings"] == [
        {"model": "arcface_r100", "variant": "orig", "dim": 512, "model_version": "v1"}
    ]
    # Newest run wins, with the person it mapped to resolved to a name.
    assert face["cluster"]["run_id"] == 2
    assert face["cluster"]["cluster_id"] == 9
    assert face["cluster"]["mapped_person_name"] == "Alice"
    assert data["extract"] == {
        "cache_key": "ck-1",
        "pipeline_version": "pv1",
        "face_count": 1,
        "processed_at": pytest.approx(now, abs=5),
        "stale": False,
    }
    assert [it["item_id"] for it in data["review_items"]] == [1]
    assert data["review_items"][0]["face_ids"] == [face_id]


def test_report_flags_a_stale_extract_row(client, db):
    _photo(db, cache_key="ck-new")
    db.execute(
        "INSERT INTO extract_log (space, photo_id, cache_key, pipeline_version, "
        "face_count, processed_at) VALUES ('personal',1,'ck-old','pv1',0,?)",
        (store.now(),),
    )
    db.commit()
    assert client.get("/api/inspect/personal/1").json()["extract"]["stale"] is True


def test_report_404s_for_a_photo_not_in_the_library(client, db):
    r = client.get("/api/inspect/personal/404")
    assert r.status_code == 404
    assert "not in the local library" in r.json()["error"]


def test_report_rejects_an_unknown_space(client, db):
    assert client.get("/api/inspect/nope/1").status_code == 422


def test_report_requires_auth(app, db):
    auth.create_user(db, "admin", "password123")
    with TestClient(app, follow_redirects=False) as anon:
        assert anon.get("/api/inspect/personal/1").status_code == 401


# --------------------------------------------------------------------------- #
# display frame
# --------------------------------------------------------------------------- #
class _Row(dict):
    """Stand-in for a photos row (key access is all display_size uses)."""


def _box(x, y, w, h):
    return _Row(x=x, y=y, w=w, h=h)


def test_display_size_ignores_the_exif_orientation():
    # Synology reports width/height for the frame it *displays* and hands over
    # the EXIF tag beside it, so swapping on orientation 5-8 corrects an already
    # corrected pair. Measured over a real library that rule was wrong on ~9 of
    # every 10 rotated photos, and every other reader of these columns
    # (crossref, _face_targets) has always taken them as they come.
    photo = _Row(width=4000, height=3000, orientation=6)
    assert display_size(photo, [_box(10, 10, 100, 100)]) == (4000.0, 3000.0)


def test_display_size_prefers_the_frame_that_contains_the_boxes():
    # A box outside a frame cannot have come from it, so containment is proof
    # where the stored pair is only a claim.
    photo = _Row(width=3000, height=4000, orientation=1)
    assert display_size(photo, [_box(3500, 10, 100, 100)]) == (4000.0, 3000.0)


def test_display_size_falls_back_to_the_box_extent():
    photo = _Row(width=None, height=0, orientation=None)
    assert display_size(photo, [_box(10, 20, 100, 80)]) == (110.0, 100.0)


def test_report_of_a_photo_without_resolution_still_normalizes(client, db):
    _photo(db, width=None, height=None)
    _face(db, x=0.0, y=0.0, w=100.0, h=200.0)
    data = client.get("/api/inspect/personal/1").json()
    assert (data["display"]["width"], data["display"]["height"]) == (100, 200)
    assert data["faces"][0]["box"] == {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}


# --------------------------------------------------------------------------- #
# image proxy
# --------------------------------------------------------------------------- #
def test_image_proxies_the_nas_thumbnail_privately(client, db, respx_mock):
    _photo(db)
    _mock_nas(respx_mock)
    route = respx_mock.get(f"{NAS_BASE_URL}/webapi/entry.cgi").mock(
        return_value=httpx.Response(200, content=b"\xff\xd8jpeg")
    )

    r = client.get("/api/inspect/image/personal/1")
    assert r.status_code == 200
    assert r.content == b"\xff\xd8jpeg"
    assert r.headers["cache-control"] == "private, max-age=86400"
    # The captured web-UI param shape (ADR 02): quoted type/size/cache_key.
    query = dict(route.calls.last.request.url.params)
    assert query["type"] == '"unit"'
    assert query["size"] == '"xl"'
    assert query["cache_key"] == '"ck-1"'
    assert query["id"] == "1"


def test_image_rejects_an_uncaptured_size(client, db):
    _photo(db)
    assert client.get("/api/inspect/image/personal/1?size=huge").status_code == 422


def test_image_404s_before_touching_the_nas(client, db, respx_mock):
    _mock_nas(respx_mock)
    route = respx_mock.get(f"{NAS_BASE_URL}/webapi/entry.cgi")
    assert client.get("/api/inspect/image/personal/7").status_code == 404
    assert not route.called


def test_image_falls_back_to_a_cached_original(client, db, settings, respx_mock):
    from synopticon.sync.downloads import original_path

    _photo(db)
    row = db.execute("SELECT * FROM photos WHERE space='personal' AND id=1").fetchone()
    cached = original_path(settings, row)
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(b"\xff\xd8original")

    _mock_nas(respx_mock)
    respx_mock.get(f"{NAS_BASE_URL}/webapi/entry.cgi").mock(
        return_value=httpx.Response(500)
    )

    r = client.get("/api/inspect/image/personal/1")
    assert r.status_code == 200
    assert r.content == b"\xff\xd8original"


def test_image_502s_when_the_nas_fails_and_nothing_is_cached(client, db, respx_mock):
    _photo(db)
    _mock_nas(respx_mock)
    respx_mock.get(f"{NAS_BASE_URL}/webapi/entry.cgi").mock(
        return_value=httpx.Response(500)
    )
    assert client.get("/api/inspect/image/personal/1").status_code == 502


def test_meta_lists_the_configured_spaces(client):
    data = client.get("/api/inspect/meta").json()
    assert data["spaces"] == ["personal"]
    assert "pipeline_version" in data


# --------------------------------------------------------------------------- #
# tagging a face nothing proposed
# --------------------------------------------------------------------------- #
def _person(db, pid=7, name="Alice"):
    db.execute(
        "INSERT INTO persons (id, space, name, synced_at) VALUES (?,'personal',?,?)",
        (pid, name, store.now()),
    )
    db.commit()


def _syno_face(db, sfid=55, photo_id=1, person_id=7, box=(0.1, 0.1, 0.18, 0.26)):
    db.execute(
        "INSERT INTO syno_faces (space, syno_face_id, photo_id, person_id, name, "
        "x1, y1, x2, y2, synced_at) VALUES ('personal',?,?,?,NULL,?,?,?,?,?)",
        (sfid, photo_id, person_id, *box, store.now()),
    )
    db.commit()


def _queued(db, item_id):
    return db.execute(
        "SELECT kind, status, confidence, payload_json, decided_by FROM review_queue "
        "WHERE item_id = ?",
        (item_id,),
    ).fetchone()


def test_assign_queues_an_approved_row_for_a_face_nobody_proposed(client, db):
    _photo(db)
    _person(db)
    face_id = _face(db)

    r = client.post(
        f"/api/inspect/face/{face_id}/assign",
        json={"space": "personal", "person_id": 7, "person_name": "Alice"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "assign"
    assert body["status"] == "approved"
    assert body["superseded"] == []

    row = _queued(db, body["item_id"])
    assert (row["kind"], row["status"], row["decided_by"]) == (
        "assign",
        "approved",
        "inspect",
    )
    # The stored score would be a similarity to a person nobody suggested.
    assert row["confidence"] is None
    payload = json.loads(row["payload_json"])
    assert payload["face_id"] == face_id
    assert payload["person_id"] == 7
    assert payload["manual_target"] is True
    assert payload["source"] == "inspect"
    # x=100 y=50 w=80 h=80 over a 1000x500 photo.
    assert payload["bbox_normalized"] == pytest.approx([0.1, 0.1, 0.18, 0.26])
    assert "syno_face_id" not in payload


def test_assign_over_a_face_synology_already_named_is_a_reassign(client, db):
    _photo(db)
    _person(db, 7, "Alice")
    _person(db, 8, "Bob")
    face_id = _face(db)
    _syno_face(db, person_id=8)

    body = client.post(
        f"/api/inspect/face/{face_id}/assign",
        json={"space": "personal", "person_id": 7, "person_name": "Alice"},
    ).json()
    # `reassign` moves Synology's own box instead of adding a second one, and is
    # the tier `apply` writes only under --apply-reassigns (ADR 05).
    assert body["kind"] == "reassign"
    payload = json.loads(_queued(db, body["item_id"])["payload_json"])
    assert payload["syno_face_id"] == 55
    assert payload["from_person_id"] == 8
    assert payload["person_id"] == 7


def test_assign_to_the_person_synology_already_named_is_refused(client, db):
    _photo(db)
    _person(db)
    face_id = _face(db)
    _syno_face(db, person_id=7)

    r = client.post(
        f"/api/inspect/face/{face_id}/assign",
        json={"space": "personal", "person_id": 7, "person_name": "Alice"},
    )
    assert r.status_code == 409
    assert db.execute("SELECT COUNT(*) AS n FROM review_queue").fetchone()["n"] == 0


def test_assign_supersedes_a_queued_suggestion_about_the_same_face(client, db):
    _photo(db)
    _person(db, 7, "Alice")
    _person(db, 8, "Bob")
    face_id = _face(db)
    other = _face(db, x=400.0)
    now = store.now()
    cur = db.execute(
        "INSERT INTO review_queue (kind, payload_json, confidence, status, created_at) "
        "VALUES ('assign', ?, 0.81, 'pending', ?)",
        (json.dumps({"space": "personal", "photo_id": 1, "face_id": face_id,
                     "person_id": 8}), now),
    )
    stale = int(cur.lastrowid)
    # A group claim naming this face among others is not about this face alone.
    cur = db.execute(
        "INSERT INTO review_queue (kind, payload_json, status, created_at) "
        "VALUES ('new_person', ?, 'pending', ?)",
        (json.dumps({"face_ids": [face_id, other], "size": 2}), now),
    )
    group = int(cur.lastrowid)
    db.commit()

    body = client.post(
        f"/api/inspect/face/{face_id}/assign",
        json={"space": "personal", "person_id": 7, "person_name": "Alice"},
    ).json()
    assert body["superseded"] == [stale]

    retired = _queued(db, stale)
    # `hidden`, not deleted: it keeps (face_id, 8) registered so the next
    # grouping run does not propose the overruled person again (ADR 14).
    assert retired["status"] == "hidden"
    assert json.loads(retired["payload_json"])["superseded_by"] == body["item_id"]
    assert _queued(db, group)["status"] == "pending"


def test_assign_refuses_a_person_in_another_space(client, db):
    _photo(db)
    face_id = _face(db)
    r = client.post(
        f"/api/inspect/face/{face_id}/assign",
        json={"space": "shared", "person_id": 7, "person_name": "Alice"},
    )
    assert r.status_code == 422
    assert db.execute("SELECT COUNT(*) AS n FROM review_queue").fetchone()["n"] == 0


def test_assign_refuses_a_photo_with_no_stored_resolution(client, db):
    _photo(db, width=None, height=None)
    face_id = _face(db)
    r = client.post(
        f"/api/inspect/face/{face_id}/assign",
        json={"space": "personal", "person_id": 7},
    )
    assert r.status_code == 422


def test_assign_404s_on_an_unknown_face(client, db):
    _photo(db)
    r = client.post(
        "/api/inspect/face/999/assign", json={"space": "personal", "person_id": 7}
    )
    assert r.status_code == 404


def test_report_names_the_synology_box_over_each_face(client, db):
    _photo(db)
    _person(db, 8, "Bob")
    face_id = _face(db)
    _syno_face(db, person_id=8)
    # Far away from our detection, so it pairs with nothing.
    _syno_face(db, sfid=56, person_id=None, box=(0.8, 0.8, 0.9, 0.95))

    (face,) = client.get("/api/inspect/personal/1").json()["faces"]
    assert face["face_id"] == face_id
    assert face["syno"]["syno_face_id"] == 55
    assert face["syno"]["person_id"] == 8
    assert face["syno"]["name"] == "Bob"
    assert face["syno"]["iou"] == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# frame rotation
# --------------------------------------------------------------------------- #
def _box(x, y, w, h):
    return {"x": x, "y": y, "w": w, "h": h}


def test_rotate_box_is_a_quarter_turn_that_swaps_the_sides():
    box = _box(0.1, 0.2, 0.3, 0.4)
    # Clockwise: the top-left corner goes to the top-right, and the sides swap
    # because the frame they are a fraction of swaps too.
    assert rotate_box(box, 90) == pytest.approx(_box(0.4, 0.1, 0.4, 0.3), abs=1e-9)
    assert rotate_box(box, 180) == pytest.approx(_box(0.6, 0.4, 0.3, 0.4), abs=1e-9)
    assert rotate_box(box, 270) == pytest.approx(_box(0.2, 0.6, 0.4, 0.3), abs=1e-9)
    # Four turns is the identity, which is what makes the manual stepper safe.
    turned = box
    for _ in range(4):
        turned = rotate_box(turned, 90)
    assert turned == pytest.approx(box, abs=1e-9)


class _Photo(dict):
    """A photos row stand-in: resolve_frame only reads three columns."""


def _syno_row(x1, y1, x2, y2):
    return {"x1": x1, "y1": y1, "x2": x2, "y2": y2}


def _face_row(x, y, w, h):
    return {"x": x, "y": y, "w": w, "h": h}


def test_frame_candidates_lead_with_the_stored_pair_whatever_exif_says():
    photo = _Photo(width=2316, height=3088, orientation=6)
    faces = [_face_row(100, 50, 80, 80)]
    assert frame_candidates(photo, faces) == ((2316.0, 3088.0), (3088.0, 2316.0))


def test_frame_candidates_let_containment_reorder_the_pair():
    # A box reaching x=2480 cannot have come from a frame only 2316 wide.
    photo = _Photo(width=2316, height=3088, orientation=1)
    faces = [_face_row(2400, 100, 80, 80)]
    assert frame_candidates(photo, faces)[0] == (3088.0, 2316.0)


def test_resolve_frame_needs_synology_faces_to_vote_with():
    photo = _Photo(width=1000, height=500, orientation=1)
    faces = [_face_row(100, 50, 80, 80)]
    assert resolve_frame(photo, faces, []) == ((1000.0, 500.0), 0, "none")
    assert resolve_frame(photo, [], [_syno_row(0.1, 0.1, 0.2, 0.2)]) == (
        (1000.0, 500.0),
        0,
        "none",
    )


def test_resolve_frame_confirms_a_seed_the_boxes_agree_with():
    # The photo 60251 shape, now that orientation gets no vote: the seed is
    # already right, and Synology's one face says so.
    photo = _Photo(width=2316, height=3088, orientation=6)
    faces = [_face_row(1000.3, 534.4, 92.4, 100.1)]
    syno = [_syno_row(0.4323, 0.1766, 0.4701, 0.2049)]
    assert resolve_frame(photo, faces, syno) == ((2316.0, 3088.0), 0, "synology-faces")


def test_resolve_frame_overrules_a_seed_the_boxes_contradict():
    # Both frames contain every box, so containment cannot help; only Synology's
    # face says the pixels were divided by the wrong pair.
    photo = _Photo(width=1000, height=500, orientation=1)
    faces = [_face_row(100, 50, 80, 80)]
    assert frame_candidates(photo, faces)[0] == (1000.0, 500.0)
    # The same face normalized against the transpose: 100/500, 50/1000, ...
    syno = [_syno_row(0.2, 0.05, 0.36, 0.13)]
    assert resolve_frame(photo, faces, syno) == ((500.0, 1000.0), 0, "synology-faces")


def test_resolve_frame_finds_the_turn_that_lands_our_boxes_on_theirs():
    photo = _Photo(width=1000, height=1000, orientation=1)  # square: one frame
    faces = [_face_row(100, 200, 300, 400), _face_row(500, 100, 200, 200)]
    for degrees in (0, 90, 180, 270):
        syno = []
        for f in faces:
            b = rotate_box(
                {"x": f["x"] / 1000, "y": f["y"] / 1000, "w": f["w"] / 1000, "h": f["h"] / 1000},
                degrees,
            )
            syno.append(_syno_row(b["x"], b["y"], b["x"] + b["w"], b["y"] + b["h"]))
        assert resolve_frame(photo, faces, syno) == ((1000.0, 1000.0), degrees, "synology-faces")


def test_resolve_frame_refuses_a_turn_that_is_only_marginally_better():
    # A face near the middle of a photo very nearly overlaps itself under a
    # half-turn. Without the margin, that symmetry flips frames that were right:
    # over a real library it invented a turn on 99 photos instead of 1.
    photo = _Photo(width=1000, height=1000, orientation=1)
    faces = [_face_row(400, 395, 200, 200)]
    syno = [_syno_row(0.4, 0.405, 0.6, 0.605)]  # an exact hit for the half-turn
    frame, turn, source = resolve_frame(photo, faces, syno)
    assert (frame, turn) == ((1000.0, 1000.0), 0)
    assert source == "synology-faces"  # the seed still fits, and was checked


def test_resolve_frame_keeps_the_seed_when_nothing_overlaps():
    # Their box sits where no frame and no turn reaches: a crop or a stretch,
    # which is neither, so the report must not invent a correction — and must
    # not claim the frame was checked either.
    photo = _Photo(width=1000, height=1000, orientation=1)
    faces = [_face_row(0, 0, 50, 50)]
    syno = [_syno_row(0.5, 0.5, 0.55, 0.55)]
    assert resolve_frame(photo, faces, syno) == ((1000.0, 1000.0), 0, "none")


def test_report_normalizes_against_the_frame_synologys_boxes_chose(client, db):
    _photo(db, width=2316, height=3088, orientation=6)
    face_id = _face(db, x=1000.3, y=534.4, w=92.4, h=100.1)
    db.execute(
        "INSERT INTO syno_faces (space, syno_face_id, photo_id, person_id, name, "
        "x1, y1, x2, y2, synced_at) VALUES ('personal',55,1,NULL,NULL,?,?,?,?,?)",
        (0.4323, 0.1766, 0.4701, 0.2049, store.now()),
    )
    db.commit()

    data = client.get("/api/inspect/personal/1").json()
    assert data["display"]["width"] == 2316
    assert data["display"]["height"] == 3088
    assert data["display"]["rotation"] == 0
    assert data["display"]["rotation_source"] == "synology-faces"
    (face,) = data["faces"]
    assert face["face_id"] == face_id
    # The box the report draws now coincides with the one Synology drew.
    assert face["box"]["x"] == pytest.approx(0.4319, abs=1e-3)
    assert face["box"]["y"] == pytest.approx(0.1730, abs=1e-3)


def test_report_reports_the_turn_that_matches_synologys_boxes(client, db):
    # Square, so the frame is not in question and only the turn is.
    _photo(db, width=1000, height=1000)
    _face(db)
    turned = rotate_box({"x": 0.1, "y": 0.05, "w": 0.08, "h": 0.08}, 90)
    db.execute(
        "INSERT INTO syno_faces (space, syno_face_id, photo_id, person_id, name, "
        "x1, y1, x2, y2, synced_at) VALUES ('personal',55,1,NULL,NULL,?,?,?,?,?)",
        (
            turned["x"],
            turned["y"],
            turned["x"] + turned["w"],
            turned["y"] + turned["h"],
            store.now(),
        ),
    )
    db.commit()

    display = client.get("/api/inspect/personal/1").json()["display"]
    assert display["rotation"] == 90
    assert display["rotation_source"] == "synology-faces"


def test_report_leaves_an_agreeing_photo_alone(client, db):
    _photo(db)
    _face(db)
    db.execute(
        "INSERT INTO syno_faces (space, syno_face_id, photo_id, person_id, name, "
        "x1, y1, x2, y2, synced_at) VALUES ('personal',55,1,NULL,NULL,?,?,?,?,?)",
        (0.1, 0.1, 0.18, 0.26, store.now()),
    )
    db.commit()

    data = client.get("/api/inspect/personal/1").json()
    assert (data["display"]["width"], data["display"]["height"]) == (1000, 500)
    assert data["display"]["rotation"] == 0
    assert data["display"]["rotation_source"] == "synology-faces"
