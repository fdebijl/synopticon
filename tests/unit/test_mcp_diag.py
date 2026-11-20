"""MCP diagnostics tools, called directly over a small seeded SQLite library.

The library is laid out so each verdict has exactly one cause:

* photo 1: our face matching Alice's Synology box, a Synology box for Bob with
  nothing of ours on it (``missed``), and a low-score YOLO box of ours with no
  Synology box under it (``extra``);
* photo 3: a Synology box on a photo Detect faces never reached;
* photo 50: deleted on the NAS, carrying a Synology box, one of our faces and a
  review item — all of which must stay out of every count;
* photos 100+: one face each, labelled through a Synology box or unlabelled,
  grouped by a hand-written cluster run in which Alice is split over two
  clusters and one of them also holds Bob.
"""

from __future__ import annotations

import io
import json
from contextlib import contextmanager

import numpy as np
import pytest

from synopticon.config import load_settings
from synopticon.db import store
from synopticon.review.lookups import VoidedCache
from synopticon.web.mcp import tools_diag
from synopticon.web.mcp.registry import McpToken, ToolContext, ToolError, ToolOutput

ALICE, BOB, GHOST = 10, 11, 99
MODELS = ("adaface_ir101", "arcface_r100")


def _unit(v):
    v = np.asarray(v, dtype=np.float32)
    return v / np.linalg.norm(v)


@pytest.fixture(autouse=True)
def _fresh_caches():
    for memo in (
        tools_diag._LIBRARY,
        tools_diag._FUSED,
        tools_diag._GRAPH,
        tools_diag._CENTROIDS,
        tools_diag._RUN,
    ):
        memo.clear()
    yield


@pytest.fixture
def settings(tmp_path):
    return load_settings(storage={"data_dir": tmp_path})


class Library:
    def __init__(self, conn, settings):
        self.conn = conn
        self.settings = settings
        self.faces: dict[str, int] = {}

    def photo(self, pid, w=1000, h=1000, deleted=0, type_="photo", detected=True):
        self.conn.execute(
            "INSERT INTO photos (id, space, filename, type, cache_key, width, height, "
            "orientation, synced_at, deleted) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (pid, "personal", f"IMG_{pid}.jpg", type_, f"ck{pid}", w, h, 1, store.now(), deleted),
        )
        if detected:
            self.conn.execute(
                "INSERT INTO extract_log (space, photo_id, cache_key, pipeline_version, "
                "face_count, processed_at) VALUES ('personal', ?, ?, 'pv1', 0, ?)",
                (pid, f"ck{pid}", store.now()),
            )

    def face(self, name, pid, box=(100, 100, 200, 200), score=0.9, detector="merged", vec=None):
        x, y, w, h = box
        cur = self.conn.execute(
            "INSERT INTO faces (space, photo_id, detector, x, y, w, h, det_score, quality, "
            "crop_path, pipeline_version, created_at) VALUES ('personal',?,?,?,?,?,?,?,?,?,?,?)",
            (pid, detector, x, y, w, h, score, 25.0, f"c/{name}.jpg", "pv1", store.now()),
        )
        fid = int(cur.lastrowid)
        self.faces[name] = fid
        self.conn.execute(
            "UPDATE extract_log SET face_count = face_count + 1 WHERE space = 'personal' "
            "AND photo_id = ?",
            (pid,),
        )
        if vec is not None:
            v = _unit(vec)
            for model in MODELS:
                self.conn.execute(
                    "INSERT INTO embeddings (face_id, model, variant, dim, vec, created_at) "
                    "VALUES (?,?,'orig',?,?,?)",
                    (fid, model, int(v.shape[0]), store.vec_to_blob(v), store.now()),
                )
        return fid

    def syno(self, sid, pid, person, box=(0.1, 0.1, 0.3, 0.3)):
        self.conn.execute(
            "INSERT INTO syno_faces (space, syno_face_id, photo_id, person_id, x1, y1, x2, "
            "y2, synced_at) VALUES ('personal',?,?,?,?,?,?,?,?)",
            (sid, pid, person, *box, store.now()),
        )

    def person(self, pid, name):
        self.conn.execute(
            "INSERT INTO persons (id, space, name, item_count, show, synced_at) "
            "VALUES (?, 'personal', ?, 5, 1, ?)",
            (pid, name, store.now()),
        )


VECTORS = {
    "a1": [1, 0.05, 0, 0],
    "a2": [1, 0.10, 0, 0],
    "a3": [1, 0.30, 0, 0],
    "a4": [1, 0.35, 0, 0],
    "b1": [0.3, 1, 0, 0],
    "b2": [0, 1, 0.10, 0],
    "b3": [0, 1, 0.15, 0],
    "u1": [0.05, 1, 0.1, 0],
    "u2": [0, 0, 0, 1],
    "u3": [0, 0, 1, 0.01],
    "u4": [0, 0, 1, 0.02],
    "u5": [0, 0, 1, 0.03],
    "u6": [0, 0, 1, 0.04],
    "u7": [0, 0, 1, 0.05],
}
LABEL = {"a1": ALICE, "a2": ALICE, "a3": ALICE, "a4": ALICE, "b1": BOB, "b2": BOB, "b3": BOB}
CLUSTERS = {
    0: (["a1", "a2"], ALICE, 1.0),
    1: (["a3", "a4", "b1"], ALICE, 2 / 3),
    2: (["b2", "b3", "u1"], BOB, 1.0),
    3: (["u2"], None, None),
    4: (["u3", "u4", "u5", "u6", "u7"], None, None),
}


@pytest.fixture
def lib(settings):
    conn = store.connect(settings)
    L = Library(conn, settings)
    L.person(ALICE, "Alice")
    L.person(BOB, "Bob")

    L.photo(1)
    L.face("p1_alice", 1, vec=[1, 0.07, 0, 0])
    L.face("p1_extra", 1, box=(600, 100, 50, 50), score=0.3, detector="yolo")
    L.syno(1, 1, ALICE, (0.1, 0.1, 0.3, 0.3))
    L.syno(2, 1, BOB, (0.6, 0.6, 0.8, 0.8))

    L.photo(2)
    L.face("p2_extra", 2, box=(700, 700, 30, 30), score=0.5, detector="scrfd")
    L.syno(3, 2, ALICE, (0.1, 0.1, 0.3, 0.3))
    L.face("p2_alice", 2, vec=[1, 0.08, 0, 0])

    L.photo(3, detected=False)
    L.syno(4, 3, BOB)

    L.photo(50, deleted=1)
    L.face("deleted", 50, box=(600, 600, 100, 100), vec=[1, 0.06, 0, 0])
    L.syno(5, 50, BOB, (0.1, 0.1, 0.3, 0.3))

    L.photo(60, detected=False)
    conn.execute(
        "INSERT INTO extract_skips (space, photo_id, cache_key, pipeline_version, reason, "
        "skipped_at) VALUES ('personal', 60, 'ck60', 'pv1', 'the file is not a decodable image', 5)"
    )
    L.photo(61, detected=False)
    conn.execute(
        "INSERT INTO extract_skips (space, photo_id, cache_key, pipeline_version, reason, "
        "skipped_at) VALUES ('personal', 61, 'old-key', 'pv1', 'the file is not a decodable image', 4)"
    )

    for i, (name, vec) in enumerate(VECTORS.items()):
        pid = 100 + i
        L.photo(pid)
        L.face(name, pid, vec=vec)
        if name in LABEL:
            L.syno(1000 + i, pid, LABEL[name])
    # An unlisted person (Synology holds it, persons does not list it).
    L.photo(200)
    L.face("ghost", 200, vec=[1, 0.32, 0, 0])
    L.syno(2000, 200, GHOST)

    params = {
        "clustering": settings.clustering.model_dump(mode="json"),
        "crossref": settings.crossref.model_dump(mode="json"),
    }
    conn.execute(
        "INSERT INTO cluster_runs (run_id, params_json, created_at) VALUES (1, ?, ?)",
        (json.dumps(params), store.now()),
    )
    clusters = dict(CLUSTERS)
    clusters[1] = (clusters[1][0] + ["ghost"], ALICE, 2 / 3)
    clusters[0] = (clusters[0][0] + ["p1_alice", "p2_alice"], ALICE, 1.0)
    clusters[5] = (["deleted"], None, None)
    for cid, (names, person, vote) in clusters.items():
        labelled = sum(1 for n in names if n in LABEL or n in ("ghost", "p1_alice", "p2_alice"))
        conn.execute(
            "INSERT INTO clusters (run_id, cluster_id, size, mapped_person_id, map_space, "
            "vote_fraction, labeled_count) VALUES (1,?,?,?,?,?,?)",
            (cid, len(names), person, "personal" if person else None, vote, labelled),
        )
        for n in names:
            conn.execute(
                "INSERT INTO cluster_members (run_id, cluster_id, face_id) VALUES (1,?,?)",
                (cid, L.faces[n]),
            )

    def item(kind, payload, status="pending"):
        conn.execute(
            "INSERT INTO review_queue (run_id, kind, payload_json, confidence, status, "
            "created_at) VALUES (1,?,?,0.7,?,?)",
            (kind, json.dumps(payload), status, store.now()),
        )

    item("assign", {"face_id": L.faces["u1"], "photo_id": 107, "space": "personal",
                    "person_id": BOB, "person_name": "Bob"})
    item("assign", {"face_id": L.faces["deleted"], "photo_id": 50, "space": "personal",
                    "person_id": BOB, "person_name": "Bob"})
    conn.commit()
    yield L
    conn.close()


@pytest.fixture
def ctx(settings):
    return ToolContext(
        settings=settings,
        conn=lambda: store.connect(settings),
        token=McpToken(1, "test", frozenset({"read"})),
        job_manager=None,
        voided=VoidedCache(),
    )


def call(tool, ctx, **args):
    return tool.handler(ctx, args)


# --------------------------------------------------------------------------- #
# registry shape                                                                #
# --------------------------------------------------------------------------- #
def test_tools_are_read_scoped_and_uniquely_named():
    names = [t.name for t in tools_diag.TOOLS]
    assert len(names) == len(set(names)) == 10
    for t in tools_diag.TOOLS:
        assert t.scopes == frozenset({"read"})
        assert t.read_only and not t.destructive
        json.dumps(t.listing())


# --------------------------------------------------------------------------- #
# detection                                                                     #
# --------------------------------------------------------------------------- #
def test_detection_summary_counts_and_agreement(lib, ctx):
    out = call(tools_diag.detection_summary, ctx)
    json.dumps(out)
    photos = out["photos"]
    assert photos["skipped_unreadable"]["total"] == 1
    assert photos["skipped_unreadable"]["by_reason"] == {"the file is not a decodable image": 1}
    # photo 3 and photo 61 (its skip row is stale) are pending; the deleted one is not counted.
    assert photos["pending"]["never_detected"] == 2
    agree = out["synology_agreement"]
    assert agree["missed"] == 1  # Bob's box on photo 1
    assert agree["on_photos_not_yet_detected"] == 1
    assert agree["unmatched_by_detector"] == {"yolo": 1, "scrfd": 1}
    # The deleted photo's face is nowhere in the face counts.
    total = len(VECTORS) + 5  # + p1 x2, p2 x2, ghost
    assert out["faces"]["total"] == total
    assert out["settings"]["detection"]["min_face_px"] == ctx.settings.detection.min_face_px


def test_missed_lists_the_synology_box_without_a_detection(lib, ctx):
    out = call(tools_diag.find_detection_gaps, ctx, kind="missed")
    assert out["total"] == 1
    item = out["items"][0]
    assert (item["space"], item["photo_id"], item["syno_face_id"]) == ("personal", 1, 2)
    assert item["synology_person"] == {
        "space": "personal", "person_id": BOB, "name": "Bob", "listed": True
    }
    assert item["box_min_side_px"] == 200.0
    # Photo 50 (deleted) has an unmatched Bob box too; it must not appear.
    assert all(i["photo_id"] != 50 for i in out["items"])


def test_extra_sorted_by_score_and_limit_respected(lib, ctx):
    out = call(tools_diag.find_detection_gaps, ctx, kind="extra", limit=1)
    assert out["total"] == 2
    assert len(out["items"]) == 1
    assert out["items"][0]["face_id"] == lib.faces["p1_extra"]  # 0.3 before 0.5
    page2 = call(tools_diag.find_detection_gaps, ctx, kind="extra", limit=1, offset=1)
    assert page2["items"][0]["face_id"] == lib.faces["p2_extra"]


def test_low_score_and_small_faces(lib, ctx):
    low = call(tools_diag.find_detection_gaps, ctx, kind="low_score", max_score=0.6)
    assert {i["face_id"] for i in low["items"]} == {lib.faces["p1_extra"], lib.faces["p2_extra"]}
    small = call(tools_diag.find_detection_gaps, ctx, kind="small_faces", max_px=40)
    assert [i["face_id"] for i in small["items"]] == [lib.faces["p2_extra"]]
    with pytest.raises(ToolError):
        call(tools_diag.find_detection_gaps, ctx, kind="low_score", max_score="high")


def test_frame_mismatch_is_not_reported_as_missed(lib, ctx, settings):
    # Stored resolution transposed relative to the frame we detected in: our
    # pixel box divided by the wrong pair misses Synology's box, but the
    # Inspect frame search re-seats it.
    lib.photo(300, w=500, h=1000)
    lib.face("wide", 300, box=(700, 100, 200, 200))
    lib.face("wide2", 300, box=(100, 300, 150, 150))
    lib.syno(3000, 300, ALICE, (0.7, 0.2, 0.9, 0.6))
    lib.syno(3001, 300, BOB, (0.1, 0.6, 0.25, 0.9))
    lib.conn.commit()
    missed = call(tools_diag.find_detection_gaps, ctx, kind="missed")
    assert all(i["photo_id"] != 300 for i in missed["items"])
    fixed = call(tools_diag.find_detection_gaps, ctx, kind="frame_mismatch")
    assert {i["syno_face_id"] for i in fixed["items"]} == {3000, 3001}
    assert fixed["items"][0]["correction"]["frame"] == [1000.0, 500.0]


def test_skips_list_and_stale_rows(lib, ctx):
    out = call(tools_diag.list_extract_skips, ctx)
    assert [i["photo_id"] for i in out["items"]] == [60]
    both = call(tools_diag.list_extract_skips, ctx, include_stale=True)
    assert [(i["photo_id"], i["still_applies"]) for i in both["items"]] == [(60, True), (61, False)]
    via_gaps = call(tools_diag.find_detection_gaps, ctx, kind="skipped")
    assert via_gaps["total"] == 1


def test_inspect_photo_trims_the_report(lib, ctx):
    out = call(tools_diag.inspect_photo, ctx, space="personal", photo_id=1)
    json.dumps(out)
    assert out["synology_faces_unmatched"] == 1
    by_id = {s["syno_face_id"]: s for s in out["synology_faces"]}
    assert by_id[1]["matched_face_id"] == lib.faces["p1_alice"]
    assert by_id[2]["matched_face_id"] is None
    face = next(f for f in out["faces"] if f["face_id"] == lib.faces["p1_alice"])
    assert face["synology_match"]["person"]["name"] == "Alice"
    assert face["cluster"]["cluster_id"] == 0
    assert face["embedding_models"] == sorted(MODELS)
    with pytest.raises(ToolError):
        call(tools_diag.inspect_photo, ctx, space="personal", photo_id=999)


def test_inspect_photo_flags_voided_review_items(lib, ctx):
    out = call(tools_diag.inspect_photo, ctx, space="personal", photo_id=50)
    assert out["photo"]["deleted"] is True
    assert [r["voided"] for r in out["review_items"]] == [True]


# --------------------------------------------------------------------------- #
# render_photo                                                                  #
# --------------------------------------------------------------------------- #
def _jpeg(w=400, h=400) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), (90, 90, 90)).save(buf, format="JPEG")
    return buf.getvalue()


class _StubClient:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.calls = []

    def api_name(self, space, suffix):
        return f"SYNO.Foto.{suffix}"

    def version_for(self, api, default):
        return default

    def stream(self, api, method, **params):
        self.calls.append((api, method, params))
        yield self.payload


class _StubSession:
    def __init__(self, client):
        self.client = client

    @contextmanager
    def use(self):
        yield self.client


def test_render_photo_draws_on_the_thumbnail(lib, ctx):
    from PIL import Image

    client = _StubClient(_jpeg())
    ctx.nas_session = _StubSession(client)
    out = call(tools_diag.render_photo, ctx, space="personal", photo_id=1, max_side=256)
    assert isinstance(out, ToolOutput)
    assert len(out.images) == 1
    img = Image.open(io.BytesIO(out.images[0].data))
    assert img.format == "JPEG" and max(img.size) == 256
    assert out.images[0].caption
    legend = out.structured["legend"]
    assert {o["face_id"] for o in legend["ours"]} == {lib.faces["p1_alice"], lib.faces["p1_extra"]}
    assert {s["syno_face_id"] for s in legend["synology"]} == {1, 2}
    assert client.calls and client.calls[0][2]["id"] == 1
    json.dumps(out.structured)


def test_render_photo_needs_a_nas_session(lib, ctx):
    ctx.nas_session = None
    with pytest.raises(ToolError, match="NAS"):
        call(tools_diag.render_photo, ctx, space="personal", photo_id=1)


# --------------------------------------------------------------------------- #
# grouping                                                                      #
# --------------------------------------------------------------------------- #
def test_grouping_summary(lib, ctx):
    out = call(tools_diag.grouping_summary, ctx)
    json.dumps(out)
    assert out["run"]["run_id"] == 1 and out["run"]["is_latest"]
    assert out["clusters"]["total"] == 6
    assert out["clusters"]["singletons"] == 2
    assert out["mapping"]["mapped_clusters"] == 3
    assert out["labels"]["persons_split_over_several_clusters"] >= 1
    assert out["labels"]["clusters_with_several_persons"] == 1
    # The voided assign on the deleted photo is not counted.
    assert out["review_items_from_run"] == {"assign": {"pending": 1}}
    assert "edge_threshold" in out["settings"]["clustering"]
    assert "majority" in out["settings"]["crossref"]


def test_split_persons_finds_alice(lib, ctx):
    out = call(tools_diag.find_grouping_issues, ctx, kind="split_persons")
    alice = next(i for i in out["items"] if i["person"]["person_id"] == ALICE)
    assert alice["clusters"] == 2
    assert alice["main_cluster"]["cluster_id"] == 0
    assert alice["faces_outside_main_cluster"] == 2
    assert set(alice["sample_face_ids_outside_main"]) == {lib.faces["a3"], lib.faces["a4"]}


def test_mixed_clusters_flags_unlisted(lib, ctx):
    out = call(tools_diag.find_grouping_issues, ctx, kind="mixed_clusters")
    assert [i["cluster_id"] for i in out["items"]] == [1]
    breakdown = {b["person_id"]: b for b in out["items"][0]["breakdown"]}
    assert breakdown[ALICE]["faces"] == 2 and breakdown[ALICE]["listed"]
    assert breakdown[GHOST]["listed"] is False
    listed = call(tools_diag.find_grouping_issues, ctx, kind="mixed_clusters", listed_only=True)
    assert listed["items"][0]["distinct_persons"] == 2


def test_large_unmapped_and_low_vote(lib, ctx):
    big = call(tools_diag.find_grouping_issues, ctx, kind="large_unmapped", min_faces=3)
    assert [i["cluster_id"] for i in big["items"]] == [4]
    assert "no face in it carries a Synology label" in big["items"][0]["why_unmapped"]
    low = call(tools_diag.find_grouping_issues, ctx, kind="low_vote", min_faces=1)
    assert [i["cluster_id"] for i in low["items"]] == [1]
    capped = call(tools_diag.find_grouping_issues, ctx, kind="split_persons", limit=1, min_faces=1)
    assert len(capped["items"]) <= 1


def test_get_cluster(lib, ctx):
    out = call(tools_diag.get_cluster, ctx, cluster_id=1)
    json.dumps(out)
    assert out["mapped_person"]["name"] == "Alice"
    assert out["labelled_faces"] == 4
    sim = out["similarity"]
    # Bob's face is the intruder: least similar to the centroid.
    assert sim["least_similar_members"][0]["face_id"] == lib.faces["b1"]
    nearest = out["nearest_clusters"][0]
    assert nearest["cluster_id"] == 0  # Alice's other cluster
    with pytest.raises(ToolError):
        call(tools_diag.get_cluster, ctx, cluster_id=77)


def test_get_cluster_excludes_deleted_members(lib, ctx):
    out = call(tools_diag.get_cluster, ctx, cluster_id=5, similarity=False)
    assert out["members_total"] == 0 and out["faces_on_deleted_photos"] == 1


def test_get_person_by_name_and_unlisted(lib, ctx):
    out = call(tools_diag.get_person, ctx, name="alice")
    assert out["person"]["person_id"] == ALICE and out["person"]["listed"]
    assert out["our_faces"]["labelled"] == 6
    assert out["grouping"]["clusters_holding_their_faces"] == 2
    assert {c["cluster_id"] for c in out["grouping"]["clusters_mapped_to_them"]} == {0, 1}

    bob = call(tools_diag.get_person, ctx, space="personal", person_id=BOB)
    assert bob["synology_boxes"]["by_verdict"]["missed"] == 1
    # The deleted photo's box and voided review item are not counted.
    assert bob["synology_boxes"]["total"] == 5
    assert bob["review_items"]["by_kind"] == {"assign": {"pending": 1}}

    ghost = call(tools_diag.get_person, ctx, space="personal", person_id=GHOST)
    assert ghost["person"]["listed"] is False
    assert "never a mapping" in ghost["note"]

    with pytest.raises(ToolError):
        call(tools_diag.get_person, ctx, name="nobody")


def test_explain_face_neighbours_by_similarity(lib, ctx):
    out = call(tools_diag.explain_face, ctx, face_id=lib.faces["a1"], k=5)
    json.dumps(out)
    sims = [n["similarity"] for n in out["neighbours"]]
    assert sims == sorted(sims, reverse=True)
    assert len(sims) == 5
    assert out["neighbours"][0]["face_id"] == lib.faces["p1_alice"]
    # The deleted photo's face is never a neighbour.
    assert all(n["face_id"] != lib.faces["deleted"] for n in out["neighbours"])
    assert out["synology"]["label"]["name"] == "Alice"
    assert out["cluster"]["cluster_id"] == 0
    assert out["neighbours"][0]["edge_kept"] is True
    nearest = out["nearest_persons"]
    assert nearest["candidates"][0]["person_id"] == ALICE
    assert all(c["listed"] for c in nearest["candidates"])


def test_explain_face_pair_and_ungrouped(lib, ctx):
    out = call(
        tools_diag.explain_face, ctx, face_id=lib.faces["a1"], other_face_id=lib.faces["u3"], k=3
    )
    pair = out["pair"]
    assert pair["same_cluster"] is False
    assert pair["above_edge_threshold"] is False
    gone = call(tools_diag.explain_face, ctx, face_id=lib.faces["deleted"])
    assert gone["neighbours"] is None
    assert any("deleted" in r for r in gone["not_grouped_because"])
    with pytest.raises(ToolError):
        call(tools_diag.explain_face, ctx, face_id=123456)


def test_explain_face_prefers_the_cached_knn_graph(lib, ctx, settings):
    from synopticon.cluster import graph

    conn = store.connect(settings)
    try:
        face_ids, X = graph.load_fused(conn, settings)
    finally:
        conn.close()
    graph.build_or_load_graph(settings, face_ids, X)
    out = call(tools_diag.explain_face, ctx, face_id=lib.faces["a1"], k=3)
    assert out["knn_source"] == "cached kNN graph"
    assert out["neighbours"][0]["face_id"] == lib.faces["p1_alice"]
    assert out["neighbours"][0]["in_this_faces_knn"] is True


def test_library_cache_follows_the_fingerprint(lib, ctx):
    call(tools_diag.detection_summary, ctx)
    first = tools_diag._LIBRARY._value
    call(tools_diag.find_detection_gaps, ctx, kind="missed")
    assert tools_diag._LIBRARY._value is first
    lib.photo(400)
    lib.face("late", 400)
    lib.conn.commit()
    call(tools_diag.detection_summary, ctx)
    assert tools_diag._LIBRARY._value is not first


def test_no_run_is_an_actionable_error(settings, ctx):
    store.connect(settings).close()
    with pytest.raises(ToolError, match="Group faces"):
        call(tools_diag.grouping_summary, ctx)
