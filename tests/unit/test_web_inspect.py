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
from synopticon.web.inspect_routes import display_size  # noqa: E402
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

    assert data["display"] == {"width": 1000, "height": 500}
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


def test_display_size_swaps_for_a_rotated_orientation():
    photo = _Row(width=4000, height=3000, orientation=6)
    assert display_size(photo, [_box(10, 10, 100, 100)]) == (3000.0, 4000.0)


def test_display_size_prefers_the_frame_that_contains_the_boxes():
    # Orientation says upright, but a box only fits the swapped frame — the
    # boxes are the ground truth, since they came out of the decoded image.
    photo = _Row(width=3000, height=4000, orientation=1)
    assert display_size(photo, [_box(3500, 10, 100, 100)]) == (4000.0, 3000.0)


def test_display_size_falls_back_to_the_box_extent():
    photo = _Row(width=None, height=0, orientation=None)
    assert display_size(photo, [_box(10, 20, 100, 80)]) == (110.0, 100.0)


def test_report_of_a_photo_without_resolution_still_normalizes(client, db):
    _photo(db, width=None, height=None)
    _face(db, x=0.0, y=0.0, w=100.0, h=200.0)
    data = client.get("/api/inspect/personal/1").json()
    assert data["display"] == {"width": 100, "height": 200}
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
