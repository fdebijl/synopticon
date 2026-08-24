"""Inspect: the per-photo debug surface (every box, every score, one photo).

Registered onto the main app by :func:`register_inspect_routes`. Three routes:

* ``GET /api/inspect/{space}/{photo_id}`` — everything the database knows about
  one photo: our detections (bbox, per-detector scores, quality, landmarks,
  crops, embeddings, cluster membership) alongside Synology's own face boxes,
  the ``extract_log`` row that produced them, and the review items that point at
  those faces. DB-only.
* ``GET /api/inspect/image/{space}/{photo_id}`` — the photo itself, proxied from
  the NAS thumbnail endpoint (read-only), falling back to a cached original.
* ``POST /api/inspect/face/{face_id}/assign`` — queue "this face is that person"
  for a face nothing proposed. Writes ``review_queue`` and nothing else; the NAS
  write still happens later through ``apply`` under its own flags (ADR 05).

Box geometry is returned **normalized** to the displayed frame, so the SPA can
overlay both sources on one image without knowing pixel dimensions: our
detections are pixel coords in the EXIF-corrected original (see
``pipeline.runner.load_image_bgr``), Synology's are already 0..1.

Which of the two candidate frames our pixels are in, and whether it is also a
quarter-turn off Synology's, is what :func:`resolve_frame` settles — against
Synology's boxes, the one piece of geometry here that was not derived from the
metadata already under suspicion.
"""

import json
import logging
from pathlib import Path
from typing import Any, Callable

from ..config import Settings
from ..db import Connection, store

log = logging.getLogger(__name__)

#: A private thumbnail is immutable for a given cache_key, so it may be held for
#: a while — but it is somebody's photo, so never by a shared cache.
_IMAGE_CACHE_CONTROL = "private, max-age=86400"

#: The only Synology thumbnail sizes the HAR captures cover (ADR 02).
_SIZES = ("sm", "xl")

#: Quarter-turns clockwise a frame can be out by. Two frames of the same photo
#: differ by one of these or by nothing — any other disagreement is a crop or a
#: stretch, which no rotation fixes.
_ROTATIONS = (0, 90, 180, 270)

#: Mean IoU, over Synology's boxes, below which nothing is landing on anything
#: and the vote has no opinion worth having.
_MIN_FIT = 0.3

#: How much better than the seed a correction must score before it is taken. A
#: single face near the middle of a photo overlaps itself under a half-turn, so
#: a bare `>` lets symmetry flip perfectly good frames; the margin is what makes
#: the vote answer "clearly better" instead of "not worse".
_MIN_MARGIN = 0.15


def _fits(faces: list[Any], width: float, height: float) -> int:
    """How many detections lie inside a ``width`` x ``height`` frame."""
    if not width or not height:
        return 0
    inside = 0
    for f in faces:
        if (
            f["x"] >= -1
            and f["y"] >= -1
            and f["x"] + f["w"] <= width + 1
            and f["y"] + f["h"] <= height + 1
        ):
            inside += 1
    return inside


def frame_candidates(photo, faces: list[Any]) -> tuple[tuple[float, float], ...]:
    """The frames our detections might live in, metadata's favourite first.

    There are only ever two, a resolution and its transpose.

    **``orientation`` deliberately gets no vote here.** It used to lead: EXIF 5-8
    say the corrected frame is rotated, so the stored resolution was swapped
    before use. Measured against Synology's own face boxes over a real library,
    that rule was wrong on roughly nine of every ten rotated photos — Synology
    reports ``width``/``height`` for the frame it *displays*, and hands over the
    EXIF tag beside it, so the swap was applied to dimensions that had already
    been corrected. Every other consumer of these columns
    (``crossref._load_face_meta``, ``queries._face_targets``) has always read
    them unswapped, and clustering works; this was the one place that did not.

    So the stored pair leads, the containment count may reorder it — it is
    proof when it fires, since a box outside a frame cannot have come from it,
    but it only fires when some face sits past the shorter side — and
    :func:`resolve_frame` settles the rest against evidence. With no resolution
    at all there is one candidate: the extent of the boxes themselves.
    """
    width = float(photo["width"] or 0)
    height = float(photo["height"] or 0)
    if width and height:
        stored = (width, height)
        swapped = (height, width)
        if _fits(faces, *swapped) > _fits(faces, *stored):
            return (swapped, stored)
        return (stored, swapped)

    extent_w = max((f["x"] + f["w"] for f in faces), default=0.0)
    extent_h = max((f["y"] + f["h"] for f in faces), default=0.0)
    return ((float(extent_w), float(extent_h)),)


def display_size(photo, faces: list[Any]) -> tuple[float, float]:
    """Metadata's best guess at the frame our detections were made against.

    The seed :func:`resolve_frame` starts from, and the answer when there is no
    evidence to check it against.
    """
    return frame_candidates(photo, faces)[0]


def rotate_box(box: dict, degrees: int) -> dict:
    """A 0..1 box turned ``degrees`` clockwise into the rotated frame.

    Width and height swap on a quarter-turn because the frame they are a
    fraction of does too, which is what keeps a rotated box the same shape in
    pixels.
    """
    x, y, w, h = box["x"], box["y"], box["w"], box["h"]
    if degrees == 90:
        return {"x": 1.0 - y - h, "y": x, "w": h, "h": w}
    if degrees == 180:
        return {"x": 1.0 - x - w, "y": 1.0 - y - h, "w": w, "h": h}
    if degrees == 270:
        return {"x": y, "y": 1.0 - x - w, "w": h, "h": w}
    return {"x": x, "y": y, "w": w, "h": h}


def _corners(box: dict) -> tuple[float, float, float, float]:
    return (box["x"], box["y"], box["x"] + box["w"], box["y"] + box["h"])


def resolve_frame(
    photo, faces: list[Any], syno_faces: list[Any]
) -> tuple[tuple[float, float], int, str]:
    """The frame our boxes live in, the turn onto Synology's, and what said so.

    Two things can put the overlay wrong, and only one of them is a rotation:

    * **The wrong divisor.** :func:`frame_candidates` guessed the transpose, so
      the same pixel boxes were divided by a swapped pair. The picture is not
      turned at all — the boxes are squashed along one axis and slide toward a
      corner. No rotation fixes this; only re-normalizing does.
    * **A real quarter-turn**, where our EXIF-corrected frame and Synology's
      upright one genuinely disagree about which way is up.

    Both are settled by the same evidence and so by one search: score every
    (frame, turn) pair by how well our boxes land on Synology's, and take the
    winner. Synology's boxes are the right arbiter because they are the only
    geometry here that was not derived from the metadata already under
    suspicion — the only geometry here derived from the pixels rather than
    from a column.

    The score is a mean IoU **over Synology's boxes**, not over ours: Synology
    labels a fraction of the faces we find (photo 60251 has one of seven), so
    averaging over ours would bury a perfect match under the faces it never
    named. Each of their boxes asks the same question — is one of our faces
    here? — and every one of them should be able to answer yes.

    A correction has to be *clearly* better than the seed, by :data:`_MIN_MARGIN`,
    and land at all, by :data:`_MIN_FIT`. Both floors earn their keep: a lone
    face near the middle of a photo overlaps itself under a half-turn, so a bare
    "best wins" lets symmetry flip frames that were already right.

    A photo where nothing lands on anything — Synology has no faces on it, which
    is common — reports the seed with ``source="none"``, and the page says so
    rather than implying the frame was checked.
    """
    from ..review.queries import box_iou

    candidates = frame_candidates(photo, faces)
    theirs = [
        (float(sf["x1"]), float(sf["y1"]), float(sf["x2"]), float(sf["y2"]))
        for sf in syno_faces
    ]
    if not theirs or not faces:
        return candidates[0], 0, "none"

    def fit(frame: tuple[float, float], degrees: int) -> float:
        width, height = frame
        if not width or not height:
            return 0.0
        ours = [
            rotate_box(
                {
                    "x": float(f["x"]) / width,
                    "y": float(f["y"]) / height,
                    "w": float(f["w"]) / width,
                    "h": float(f["h"]) / height,
                },
                degrees,
            )
            for f in faces
        ]
        return sum(
            max(box_iou(_corners(box), t) for box in ours) for t in theirs
        ) / len(theirs)

    seed = candidates[0]
    seed_score = fit(seed, 0)

    best_frame, best_turn, best_score = seed, 0, seed_score
    for frame in candidates:
        for degrees in _ROTATIONS:
            score = fit(frame, degrees)
            if score > best_score + 1e-9:
                best_frame, best_turn, best_score = frame, degrees, score

    if best_score >= _MIN_FIT and best_score - seed_score >= _MIN_MARGIN:
        return best_frame, best_turn, "synology-faces"
    # The seed stands. Whether that is a checked answer or an unchecked one is
    # the difference between "these boxes are on the faces" and "nothing here
    # disagreed with the metadata", and the page says which.
    if seed_score >= _MIN_FIT:
        return seed, 0, "synology-faces"
    return seed, 0, "none"


def _norm(x: float, y: float, w: float, h: float, size: tuple[float, float]):
    """A pixel box as a 0..1 box against ``size``, or ``None`` if size is unknown."""
    width, height = size
    if not width or not height:
        return None
    return {"x": x / width, "y": y / height, "w": w / width, "h": h / height}


def _landmarks(blob, size: tuple[float, float]) -> list[dict] | None:
    """The 5 landmark points, normalized. ``None`` when absent or malformed."""
    width, height = size
    if not blob or not width or not height:
        return None
    try:
        flat = store.blob_to_vec(bytes(blob))
    except (TypeError, ValueError):
        return None
    if flat.size < 10:
        return None
    pts = flat[:10].reshape(5, 2)
    return [{"x": float(p[0]) / width, "y": float(p[1]) / height} for p in pts]


def _person_names(conn: Connection, keys: set[tuple[str, int]]) -> dict:
    """``(space, person_id) -> name`` for the persons a report mentions."""
    out: dict[tuple[str, int], str | None] = {}
    for space in {s for s, _ in keys}:
        ids = [pid for s, pid in keys if s == space]
        if not ids:
            continue
        placeholders = ",".join("?" * len(ids))
        for row in conn.execute(
            f"SELECT id, name FROM persons WHERE space = ? AND id IN ({placeholders})",
            [space, *ids],
        ):
            out[(space, int(row["id"]))] = row["name"]
    return out


def _clusters(conn: Connection, face_ids: list[int]) -> dict[int, dict]:
    """Each face's membership in the newest cluster run that contains it."""
    if not face_ids:
        return {}
    placeholders = ",".join("?" * len(face_ids))
    rows = conn.execute(
        "SELECT cm.face_id, cm.run_id, cm.cluster_id, c.size, c.mapped_person_id, "
        "       c.map_space, c.vote_fraction, c.labeled_count "
        "FROM cluster_members cm "
        "LEFT JOIN clusters c ON c.run_id = cm.run_id AND c.cluster_id = cm.cluster_id "
        f"WHERE cm.face_id IN ({placeholders}) "
        "ORDER BY cm.run_id",
        list(face_ids),
    ).fetchall()
    # Ascending run_id, so the last write per face is the newest run.
    out: dict[int, dict] = {}
    for row in rows:
        out[int(row["face_id"])] = {
            "run_id": int(row["run_id"]),
            "cluster_id": int(row["cluster_id"]),
            "size": row["size"],
            "mapped_person_id": row["mapped_person_id"],
            "map_space": row["map_space"],
            "vote_fraction": row["vote_fraction"],
            "labeled_count": row["labeled_count"],
        }
    return out


def _embeddings(conn: Connection, face_ids: list[int]) -> dict[int, list[dict]]:
    """``face_id -> [{model, variant, dim, model_version}]`` (vectors excluded)."""
    if not face_ids:
        return {}
    placeholders = ",".join("?" * len(face_ids))
    out: dict[int, list[dict]] = {}
    for row in conn.execute(
        "SELECT face_id, model, variant, dim, model_version FROM embeddings "
        f"WHERE face_id IN ({placeholders}) ORDER BY model, variant",
        list(face_ids),
    ):
        out.setdefault(int(row["face_id"]), []).append(
            {
                "model": row["model"],
                "variant": row["variant"],
                "dim": row["dim"],
                "model_version": row["model_version"],
            }
        )
    return out


def _review_items(
    conn: Connection, space: str, photo_id: int, face_ids: set[int]
) -> list[dict]:
    """Review rows that name this photo or one of its faces.

    A full scan with a JSON parse per row, which is what `queries.orphaned_items`
    does for the same reason: the face ids live inside ``payload_json`` and
    ``json_extract`` returns text on PostgreSQL and an integer on SQLite, so
    there is no one comparison that holds on both backends.
    """
    from ..review.queries import payload_face_ids

    items = []
    for row in conn.execute(
        "SELECT item_id, kind, payload_json, confidence, status, created_at, "
        "       decided_at, decided_by FROM review_queue ORDER BY item_id"
    ):
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            continue
        try:
            same_photo = (
                str(payload.get("space")) == space
                and payload.get("photo_id") is not None
                and int(payload["photo_id"]) == photo_id
            )
        except (TypeError, ValueError):
            same_photo = False
        named = payload_face_ids(payload) & face_ids
        if not same_photo and not named:
            continue
        items.append(
            {
                "item_id": int(row["item_id"]),
                "kind": row["kind"],
                "status": row["status"],
                "confidence": row["confidence"],
                "created_at": row["created_at"],
                "decided_at": row["decided_at"],
                "decided_by": row["decided_by"],
                "face_ids": sorted(named),
                "payload": payload,
            }
        )
    return items


def photo_report(conn: Connection, settings: Settings, space: str, photo_id: int):
    """Everything the database knows about one photo, or ``None`` if unsynced."""
    from ..links import item_url, person_url, syno_web_base
    from ..review.queries import crop_url_mapper, photo_syno_match

    photo = conn.execute(
        "SELECT * FROM photos WHERE space = ? AND id = ?", (space, photo_id)
    ).fetchone()
    if photo is None:
        return None

    face_rows = conn.execute(
        "SELECT * FROM faces WHERE space = ? AND photo_id = ? ORDER BY face_id",
        (space, photo_id),
    ).fetchall()
    syno_rows = conn.execute(
        "SELECT * FROM syno_faces WHERE space = ? AND photo_id = ? ORDER BY syno_face_id",
        (space, photo_id),
    ).fetchall()
    # Which of the two candidate frames our pixels are in, and whether it is
    # also a quarter-turn off the photo the NAS serves. Both are metadata
    # questions that metadata answers badly, so they are put to Synology's boxes.
    size, rotation, rotation_source = resolve_frame(photo, face_rows, syno_rows)
    face_ids = [int(r["face_id"]) for r in face_rows]

    to_crop_url = crop_url_mapper(Path(settings.storage.crops_dir))
    clusters = _clusters(conn, face_ids)
    embeddings = _embeddings(conn, face_ids)
    # Which of Synology's boxes is this face, if any — the same pairing the
    # assign route decides `assign` vs `reassign` from, so the button a reader
    # sees names the tier the write will actually take.
    syno_match = photo_syno_match(conn, space, photo_id)

    wanted: set[tuple[str, int]] = set()
    for row in syno_rows:
        if row["person_id"] is not None:
            wanted.add((space, int(row["person_id"])))
    for entry in clusters.values():
        if entry["mapped_person_id"] is not None and entry["map_space"]:
            wanted.add((str(entry["map_space"]), int(entry["mapped_person_id"])))
    names = _person_names(conn, wanted)

    web_base = syno_web_base(settings)
    faces = []
    for row in face_rows:
        fid = int(row["face_id"])
        cluster = clusters.get(fid)
        if cluster is not None:
            cluster = dict(cluster)
            key = (str(cluster["map_space"]), cluster["mapped_person_id"])
            cluster["mapped_person_name"] = (
                names.get((key[0], int(key[1]))) if key[1] is not None else None
            )
            cluster["mapped_person_url"] = person_url(
                web_base, cluster["map_space"], cluster["mapped_person_id"]
            )
        faces.append(
            {
                "face_id": fid,
                "detector": row["detector"],
                "bbox": {
                    "x": row["x"],
                    "y": row["y"],
                    "w": row["w"],
                    "h": row["h"],
                },
                "box": _norm(row["x"], row["y"], row["w"], row["h"], size),
                "det_score": row["det_score"],
                "det_score_secondary": row["det_score_secondary"],
                "quality": row["quality"],
                "restored": bool(row["restored"]),
                "restore_disagreement": row["restore_disagreement"],
                "pipeline_version": row["pipeline_version"],
                "created_at": row["created_at"],
                "crop_url": to_crop_url(row["crop_path"]),
                "ctx_crop_url": to_crop_url(row["ctx_crop_path"]),
                "landmarks": _landmarks(row["landmarks"], size),
                "embeddings": embeddings.get(fid, []),
                "cluster": cluster,
                "syno": (
                    {
                        "syno_face_id": match["syno_face_id"],
                        "person_id": match["person_id"],
                        "name": match["name"]
                        or (
                            names.get((space, int(match["person_id"])))
                            if match["person_id"] is not None
                            else None
                        ),
                        "iou": match["iou"],
                    }
                    if (match := syno_match.get(fid)) is not None
                    else None
                ),
            }
        )

    syno_faces = []
    for row in syno_rows:
        person_id = row["person_id"]
        syno_faces.append(
            {
                "syno_face_id": int(row["syno_face_id"]),
                "person_id": person_id,
                "name": row["name"] or (names.get((space, int(person_id))) if person_id else None),
                "box": {
                    "x": row["x1"],
                    "y": row["y1"],
                    "w": row["x2"] - row["x1"],
                    "h": row["y2"] - row["y1"],
                },
                "person_url": person_url(web_base, space, person_id),
                "synced_at": row["synced_at"],
            }
        )

    extract = conn.execute(
        "SELECT cache_key, pipeline_version, face_count, processed_at "
        "FROM extract_log WHERE space = ? AND photo_id = ?",
        (space, photo_id),
    ).fetchone()

    linked_photo_id = store.link_photo_id(conn, space, photo_id)
    detection = settings.detection

    return {
        "space": space,
        "photo_id": photo_id,
        "photo": {
            "filename": photo["filename"],
            "folder_id": photo["folder_id"],
            "filesize": photo["filesize"],
            "time": photo["time"],
            "indexed_time": photo["indexed_time"],
            "type": photo["type"],
            "cache_key": photo["cache_key"],
            "unit_id": photo["unit_id"],
            "width": photo["width"],
            "height": photo["height"],
            "orientation": photo["orientation"],
            "synced_at": photo["synced_at"],
            "deleted": bool(photo["deleted"]),
            "sha256": photo["sha256"],
            "phash": photo["phash"],
            "similar_top_pick": photo["similar_top_pick"],
        },
        "display": {
            "width": size[0] or None,
            "height": size[1] or None,
            # Clockwise, applied by the SPA to our boxes only — Synology's are
            # already in the served photo's frame.
            "rotation": rotation,
            "rotation_source": rotation_source,
        },
        "image_url": f"/api/inspect/image/{space}/{photo_id}",
        "nas_url": item_url(web_base, space, linked_photo_id),
        "linked_photo_id": linked_photo_id,
        "extract": (
            {
                "cache_key": extract["cache_key"],
                "pipeline_version": extract["pipeline_version"],
                "face_count": extract["face_count"],
                "processed_at": extract["processed_at"],
                "stale": bool(extract["cache_key"] != photo["cache_key"]),
            }
            if extract is not None
            else None
        ),
        "faces": faces,
        "syno_faces": syno_faces,
        "review_items": _review_items(conn, space, photo_id, set(face_ids)),
        "detection": {
            "scrfd_score": detection.scrfd_score,
            "yolo_score": detection.yolo_score,
            "nms_iou": detection.nms_iou,
            "cross_iou": detection.cross_iou,
            "min_face_px": detection.min_face_px,
            "max_long_side": detection.max_long_side,
            "scales": list(detection.scales),
        },
    }


def register_inspect_routes(
    app,
    settings: Settings,
    conn: Callable[[], Connection],
    session,
) -> None:
    """Attach the Inspect API to ``app``.

    ``conn`` is ``create_app``'s per-request connection factory. ``session`` is
    the shared :class:`quickmerger.NasSession` — the image route is the only
    part of Inspect that talks to the NAS, and it reuses that one logged-in
    client rather than building a second one.
    """
    from fastapi import Request
    from fastapi.responses import FileResponse, JSONResponse, Response
    from starlette.concurrency import run_in_threadpool

    from ..pipeline.version import pipeline_version
    from ..syno import foto
    from ..syno.client import SynoApiError, SynoError

    spaces = list(settings.nas.spaces)

    def _space(raw: str) -> str:
        space = (raw or "").strip() or (spaces[0] if spaces else "personal")
        if space not in ("personal", "shared"):
            raise ValueError(f"unknown space {space!r}")
        return space

    def _nas_error(exc: Exception) -> JSONResponse:
        if isinstance(exc, SynoApiError):
            return JSONResponse({"error": str(exc), "code": exc.code}, status_code=502)
        return JSONResponse({"error": str(exc)}, status_code=502)

    @app.get("/api/inspect/meta")
    def api_inspect_meta():
        """What the Inspect page needs before it has an id: spaces, our version."""
        version = None
        try:
            version = pipeline_version(settings, settings.storage.models_dir)
        except Exception:
            # No model manifest on disk yet (fresh install): the page still works,
            # it just cannot say whether a face is stale.
            log.debug("inspect: pipeline_version unavailable", exc_info=True)
        return {"spaces": spaces, "pipeline_version": version}

    # Registered before the report route so "image" is never read as a space, and
    # listed in `app._NO_GZIP` — JPEG through deflate is loop CPU for nothing.
    @app.get("/api/inspect/image/{space}/{photo_id}")
    async def api_inspect_image(space: str, photo_id: int, size: str = "xl"):
        try:
            resolved = _space(space)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        if size not in _SIZES:
            return JSONResponse(
                {"error": f"size must be one of {', '.join(_SIZES)}"}, status_code=422
            )

        def photo_row():
            c = conn()
            try:
                return c.execute(
                    "SELECT * FROM photos WHERE space = ? AND id = ?",
                    (resolved, photo_id),
                ).fetchone()
            finally:
                c.close()

        row = await run_in_threadpool(photo_row)
        if row is None:
            return JSONResponse({"error": "photo not synced"}, status_code=404)

        def fetch():
            # Buffered, not streamed: a StreamingResponse would hold the NAS
            # session lock across the whole response body.
            with session.use() as client:
                return b"".join(
                    foto.download_item_thumbnail(
                        client, resolved, photo_id, row["cache_key"] or "", size
                    )
                )

        try:
            data = await run_in_threadpool(fetch)
        except SynoError as exc:
            # A cached original is the offline fallback: same bytes the pipeline
            # read, so the overlay still lines up.
            from ..sync.downloads import original_path

            cached = original_path(settings, row)
            if await run_in_threadpool(cached.is_file):
                return FileResponse(
                    cached, headers={"Cache-Control": _IMAGE_CACHE_CONTROL}
                )
            return _nas_error(exc)

        return Response(
            content=data,
            media_type="image/jpeg",
            headers={"Cache-Control": _IMAGE_CACHE_CONTROL},
        )

    @app.get("/api/inspect/{space}/{photo_id}")
    async def api_inspect(space: str, photo_id: int):
        try:
            resolved = _space(space)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)

        def work():
            c = conn()
            try:
                return photo_report(c, settings, resolved, photo_id)
            finally:
                c.close()

        report = await run_in_threadpool(work)
        if report is None:
            return JSONResponse(
                {"error": f"photo {photo_id} is not in the local library"},
                status_code=404,
            )
        return report

    @app.post("/api/inspect/face/{face_id}/assign")
    async def api_inspect_assign(request: Request, face_id: int):
        """Tag a face the pipeline never proposed as the person a human picked.

        Queue-only, like the review cards' Reassign (ADR 14): it writes one
        approved row and `apply` still holds the flags that reach the NAS. The
        tier that row lands in is `queries.assign_face`'s call, not the
        caller's — a face Synology has already named becomes a `reassign`, which
        `apply` writes only under its own flag.
        """
        from ..review import queries

        body = await request.json()
        space = (body.get("space") or "").strip()
        person_id = body.get("person_id")
        person_name = (body.get("person_name") or "").strip()
        if not space or not isinstance(person_id, int):
            return JSONResponse(
                {"error": "space (str) and person_id (int) are required"},
                status_code=422,
            )
        try:
            resolved = _space(space)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)

        def work():
            c = conn()
            try:
                return queries.assign_face(
                    c, face_id, resolved, person_id, person_name
                )
            finally:
                c.close()

        try:
            result = await run_in_threadpool(work)
        except queries.AlreadyTagged as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except (queries.SpaceMismatch, queries.MissingGeometry) as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        if result is None:
            return JSONResponse(
                {"error": f"face {face_id} is not in the local library"},
                status_code=404,
            )
        return {"face_id": face_id, **result}
