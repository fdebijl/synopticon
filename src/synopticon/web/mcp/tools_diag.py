"""Face detection and face grouping diagnostics for the MCP server.

Read-only tools that let an agent work out *why* a face was missed, boxed
wrongly, split across groups or grouped with someone else, and which setting
to change about it. Everything here is database work plus, for
``render_photo``, one thumbnail fetch through Inspect's shared NAS session.

Verdicts are computed the way the pipeline computes them, so they agree with
what clustering saw: our boxes are paired with Synology's by
``crossref.match_boxes`` on boxes normalised by ``photos.width``/``height``,
labels come from ``crossref.label_faces_with_ids``, similarities from
``graph.load_fused`` with the fusion weights of the run being explained.
Synology's unlisted persons are reported but flagged ``listed: false``; they
are never a mapping target (ADR 19, ADR 20).

The O(library) work (every face, box and label; the fused embeddings) is held
in module-level caches keyed on a cheap aggregate fingerprint, because an agent
iterating on a question calls these tools back to back. The fused matrix is
large, so it is dropped after a few idle minutes.
"""

from __future__ import annotations

import io
import json
import logging
import math
import threading
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, NamedTuple

import numpy as np

from ...db import Connection, errors as db_errors
from .registry import (
    ImageContent,
    Tool,
    ToolContext,
    ToolError,
    ToolOutput,
    arg_bool,
    arg_int,
    arg_str,
    tool,
)

log = logging.getLogger(__name__)

PersonKey = tuple[str, int]

_SPACES = ("personal", "shared")
_DEFAULT_LIMIT = 25
_MAX_LIMIT = 200

#: How long the fused embedding matrix (hundreds of MB on a large library) and
#: the kNN graph stay resident after the last tool that used them.
_IDLE_SECONDS = 600.0

_SCORE_EDGES = (0.0, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)
_PX_EDGES = (0.0, 20.0, 30.0, 40.0, 60.0, 80.0, 120.0, 200.0, 400.0)
_VOTE_EDGES = (0.0, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0)
_SIZE_BUCKETS = ((1, 1), (2, 2), (3, 5), (6, 10), (11, 50), (51, 200), (201, None))

#: A detection only YOLO made. Crossref neither scores nor uses it as evidence
#: for the nearest-person step (ADR 19).
_UNCONFIRMED_DETECTOR = "yolo"
#: crossref's per-person top-k mean for the nearest-person step.
_NEAREST_TOPK = 5


# --------------------------------------------------------------------------- #
# Small helpers                                                                 #
# --------------------------------------------------------------------------- #
def _r(value: Any, nd: int = 4) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f, nd)


def _quantiles(values: list[float], nd: int = 3) -> dict | None:
    if not values:
        return None
    arr = np.asarray(values, dtype=np.float64)
    q = np.percentile(arr, [5, 25, 50, 75, 95])
    return {
        "n": int(arr.size),
        "min": _r(arr.min(), nd),
        "p5": _r(q[0], nd),
        "p25": _r(q[1], nd),
        "median": _r(q[2], nd),
        "p75": _r(q[3], nd),
        "p95": _r(q[4], nd),
        "max": _r(arr.max(), nd),
    }


def _fmt_edge(v: float) -> str:
    return f"{v:g}"


def _hist(values: list[float], edges: tuple[float, ...]) -> list[dict]:
    """Counts per ``[edges[i], edges[i+1])`` bucket; the last is open-ended."""
    arr = np.asarray(values, dtype=np.float64)
    idx = np.searchsorted(np.asarray(edges), arr, side="right") - 1
    out = []
    below = int((idx < 0).sum())
    if below:
        out.append({"range": f"<{_fmt_edge(edges[0])}", "count": below})
    for i, lo in enumerate(edges):
        label = (
            f"{_fmt_edge(lo)}-{_fmt_edge(edges[i + 1])}"
            if i + 1 < len(edges)
            else f">={_fmt_edge(lo)}"
        )
        out.append({"range": label, "count": int((idx == i).sum())})
    return out


def _size_bucket_label(lo: int, hi: int | None) -> str:
    if hi is None:
        return f"{lo}+"
    return str(lo) if lo == hi else f"{lo}-{hi}"


def _space_arg(ctx: ToolContext, args: dict, *, required: bool = False) -> str | None:
    space = arg_str(args, "space", choices=_SPACES)
    if space is None and required:
        spaces = list(ctx.settings.nas.spaces) or ["personal"]
        return spaces[0]
    return space


def _arg_float(args: dict, key: str, default: float) -> float:
    value = args.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolError(f"{key!r} must be a number")
    return float(value)


def _limit(args: dict, default: int = _DEFAULT_LIMIT) -> int:
    return int(arg_int(args, "limit", default, lo=1, hi=_MAX_LIMIT))


@contextmanager
def _db(ctx: ToolContext) -> Iterator[Connection]:
    conn = ctx.conn()
    try:
        yield conn
    finally:
        conn.close()


def _voided(ctx: ToolContext, conn: Connection) -> frozenset[int]:
    if ctx.voided is not None:
        return ctx.voided.get(conn)
    from ...review.queries import voided_items

    return voided_items(conn)


def _tuning(ctx: ToolContext):
    """The settings the next job will run with.

    Jobs load the config file fresh, while ``ctx.settings`` is the web
    process's start-up copy, which an ``update_config`` leaves behind. Paths
    always come from ``ctx.settings``; only thresholds are read from this.
    """
    try:
        from ...config import load_settings

        return load_settings()
    except Exception:  # noqa: BLE001 - an invalid file on disk: report what runs now
        log.debug("mcp diag: config reload failed; using start-up settings", exc_info=True)
        return ctx.settings


def _pipeline_version(ctx: ToolContext, tuning) -> str | None:
    try:
        from ...pipeline.manifest import load_manifest
        from ...pipeline.version import pipeline_version

        models_dir = ctx.settings.storage.models_dir
        if not load_manifest(models_dir):
            return None
        return pipeline_version(tuning, models_dir)
    except Exception:  # noqa: BLE001 - no manifest on a fresh install
        return None


# --------------------------------------------------------------------------- #
# Caches                                                                        #
# --------------------------------------------------------------------------- #
#: Aggregates over every table the caches derive from. The last column (latest
#: cluster run) is left out of the library key, which does not depend on runs.
_FINGERPRINT_SQL = """
SELECT
  (SELECT COUNT(*) FROM faces),
  (SELECT MAX(face_id) FROM faces),
  (SELECT COUNT(*) FROM embeddings),
  (SELECT COUNT(*) FROM syno_faces),
  (SELECT MAX(syno_face_id) FROM syno_faces),
  (SELECT COALESCE(SUM(person_id), 0) FROM syno_faces),
  (SELECT COUNT(*) FROM photos),
  (SELECT MAX(id) FROM photos),
  (SELECT COUNT(*) FROM photos WHERE deleted = 1),
  (SELECT COALESCE(SUM(id), 0) FROM photos WHERE deleted = 1),
  (SELECT COUNT(*) FROM persons),
  (SELECT COALESCE(SUM(deleted), 0) FROM persons),
  (SELECT COUNT(*) FROM person_photos),
  (SELECT COUNT(*) FROM extract_log),
  (SELECT MAX(processed_at) FROM extract_log),
  (SELECT COUNT(*) FROM extract_skips),
  (SELECT MAX(skipped_at) FROM extract_skips),
  (SELECT MAX(run_id) FROM cluster_runs)
"""


def _fingerprint(conn: Connection) -> tuple | None:
    try:
        row = conn.execute(_FINGERPRINT_SQL).fetchone()
    except db_errors.DatabaseError:
        conn.rollback()
        return None
    return tuple(None if v is None else int(v) for v in row)


class _Memo:
    """One fingerprint-keyed value, built under the lock.

    Concurrent callers after an invalidation wait for one build rather than
    each running their own. A ``None`` key (fingerprint unavailable) is never
    cached. With ``idle_seconds`` the value is dropped that long after its last
    use, so a large matrix does not outlive the agent session that wanted it.
    """

    def __init__(self, idle_seconds: float | None = None) -> None:
        self._lock = threading.Lock()
        self._key: Any = None
        self._value: Any = None
        self._idle = idle_seconds
        self._timer: threading.Timer | None = None
        self._generation = 0

    def get(self, key: Any, build: Callable[[], Any]) -> Any:
        with self._lock:
            if key is not None and self._value is not None and key == self._key:
                self._arm()
                return self._value
            self._key, self._value = None, None
            value = build()
            if key is not None:
                self._key, self._value = key, value
                self._arm()
            return value

    def _arm(self) -> None:
        if self._idle is None:
            return
        if self._timer is not None:
            self._timer.cancel()
        self._generation += 1
        gen = self._generation
        timer = threading.Timer(self._idle, self._expire, args=(gen,))
        timer.daemon = True
        timer.start()
        self._timer = timer

    def _expire(self, generation: int) -> None:
        with self._lock:
            if generation == self._generation:
                self._key, self._value, self._timer = None, None, None

    def clear(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._key, self._value, self._timer = None, None, None


class _Face(NamedTuple):
    space: str
    photo_id: int
    detector: str
    x: float
    y: float
    w: float
    h: float
    det_score: float | None
    det_score_secondary: float | None
    quality: float | None
    pipeline_version: str
    has_crop: bool


class _Photo(NamedTuple):
    filename: str | None
    type: str | None
    cache_key: str | None
    width: int | None
    height: int | None
    orientation: int | None
    deleted: bool


class _Syno(NamedTuple):
    syno_face_id: int
    person_id: int | None
    name: str | None
    box: tuple[float, float, float, float]


@dataclass
class _Geometry:
    #: (space, syno_face_id) -> (verdict, our face_id or None, iou).
    #: matched | frame_mismatch | missed | not_detected
    syno: dict[tuple[str, int], tuple[str, int | None, float]] = field(default_factory=dict)
    #: face_id -> (verdict, syno_face_id or None, iou).
    #: matched | frame_mismatch | extra | no_synology_faces
    faces: dict[int, tuple[str, int | None, float]] = field(default_factory=dict)
    #: Best sub-threshold IoU for a missed Synology box: (face_id, iou).
    near_miss: dict[tuple[str, int], tuple[int, float]] = field(default_factory=dict)
    #: (space, photo_id) -> how Inspect's frame search re-seated our boxes.
    corrected: dict[tuple[str, int], dict] = field(default_factory=dict)


class _Library:
    """Every face, box, photo and person the diagnostics read, in memory."""

    def __init__(self, conn: Connection) -> None:
        self.photos: dict[tuple[str, int], _Photo] = {}
        for row in conn.execute(
            "SELECT space, id, filename, type, cache_key, width, height, orientation, "
            "deleted FROM photos"
        ):
            self.photos[(str(row["space"]), int(row["id"]))] = _Photo(
                row["filename"],
                row["type"],
                row["cache_key"],
                int(row["width"]) if row["width"] else None,
                int(row["height"]) if row["height"] else None,
                row["orientation"],
                bool(row["deleted"]),
            )

        self.faces: dict[int, _Face] = {}
        self.faces_by_photo: dict[tuple[str, int], list[int]] = defaultdict(list)
        for row in conn.execute(
            "SELECT face_id, space, photo_id, detector, x, y, w, h, det_score, "
            "det_score_secondary, quality, pipeline_version, crop_path FROM faces "
            "ORDER BY face_id"
        ):
            fid = int(row["face_id"])
            face = _Face(
                str(row["space"]),
                int(row["photo_id"]),
                str(row["detector"]),
                float(row["x"]),
                float(row["y"]),
                float(row["w"]),
                float(row["h"]),
                None if row["det_score"] is None else float(row["det_score"]),
                None
                if row["det_score_secondary"] is None
                else float(row["det_score_secondary"]),
                None if row["quality"] is None else float(row["quality"]),
                str(row["pipeline_version"]),
                bool(row["crop_path"]),
            )
            self.faces[fid] = face
            self.faces_by_photo[(face.space, face.photo_id)].append(fid)

        self.syno_by_photo: dict[tuple[str, int], list[_Syno]] = defaultdict(list)
        for row in conn.execute(
            "SELECT space, syno_face_id, photo_id, person_id, name, x1, y1, x2, y2 "
            "FROM syno_faces ORDER BY syno_face_id"
        ):
            self.syno_by_photo[(str(row["space"]), int(row["photo_id"]))].append(
                _Syno(
                    int(row["syno_face_id"]),
                    None if row["person_id"] is None else int(row["person_id"]),
                    row["name"],
                    (float(row["x1"]), float(row["y1"]), float(row["x2"]), float(row["y2"])),
                )
            )

        self.extract_log: dict[tuple[str, int], tuple[str | None, str, int]] = {}
        for row in conn.execute(
            "SELECT space, photo_id, cache_key, pipeline_version, face_count FROM extract_log"
        ):
            self.extract_log[(str(row["space"]), int(row["photo_id"]))] = (
                row["cache_key"],
                str(row["pipeline_version"]),
                int(row["face_count"]),
            )

        self.skips: dict[tuple[str, int], tuple[str | None, str, str, int]] = {}
        for row in conn.execute(
            "SELECT space, photo_id, cache_key, pipeline_version, reason, skipped_at "
            "FROM extract_skips"
        ):
            self.skips[(str(row["space"]), int(row["photo_id"]))] = (
                row["cache_key"],
                str(row["pipeline_version"]),
                str(row["reason"]),
                int(row["skipped_at"]),
            )

        self.persons: dict[PersonKey, dict] = {}
        for row in conn.execute(
            "SELECT space, id, name, item_count, show, deleted FROM persons"
        ):
            self.persons[(str(row["space"]), int(row["id"]))] = {
                "name": row["name"],
                "item_count": row["item_count"],
                "show": None if row["show"] is None else bool(row["show"]),
                "deleted": bool(row["deleted"]),
            }
        self.known: set[PersonKey] = {pk for pk, p in self.persons.items() if not p["deleted"]}

        self._lock = threading.RLock()
        self._geometry: _Geometry | None = None
        self._labels: tuple[dict[int, PersonKey], dict[int, int]] | None = None
        self._syno_by_person: dict[PersonKey, list[tuple[int, _Syno]]] | None = None

    # -- liveness ----------------------------------------------------------- #
    def photo_live(self, key: tuple[str, int]) -> bool:
        photo = self.photos.get(key)
        return photo is not None and not photo.deleted

    def face_live(self, fid: int) -> bool:
        """Mirrors ``graph.load_fused``: only a photo marked deleted rules it out."""
        face = self.faces.get(fid)
        if face is None:
            return False
        photo = self.photos.get((face.space, face.photo_id))
        return photo is None or not photo.deleted

    # -- persons ------------------------------------------------------------ #
    def syno_by_person(self) -> dict[PersonKey, list[tuple[int, _Syno]]]:
        with self._lock:
            if self._syno_by_person is None:
                out: dict[PersonKey, list[tuple[int, _Syno]]] = defaultdict(list)
                for (space, pid), boxes in self.syno_by_photo.items():
                    for sf in boxes:
                        if sf.person_id is not None:
                            out[(space, sf.person_id)].append((pid, sf))
                self._syno_by_person = out
            return self._syno_by_person

    def name_of(self, pk: PersonKey) -> str | None:
        person = self.persons.get(pk)
        if person is not None and person["name"]:
            return person["name"]
        for _, sf in self.syno_by_person().get(pk, ()):
            if sf.name:
                return sf.name
        return None

    def person(self, pk: PersonKey | None) -> dict | None:
        if pk is None:
            return None
        return {
            "space": pk[0],
            "person_id": int(pk[1]),
            "name": self.name_of(pk),
            "listed": pk in self.known,
        }

    # -- labels (crossref's ground truth) ----------------------------------- #
    def labels(self, conn: Connection, settings) -> tuple[dict[int, PersonKey], dict[int, int]]:
        with self._lock:
            if self._labels is None:
                from ...cluster import crossref

                self._labels = crossref.label_faces_with_ids(conn, settings)
            return self._labels

    # -- geometry ----------------------------------------------------------- #
    def geometry(self) -> _Geometry:
        with self._lock:
            if self._geometry is None:
                self._geometry = self._build_geometry()
            return self._geometry

    def _build_geometry(self) -> _Geometry:
        from ...cluster.crossref import _iou, match_boxes
        from ..inspect_routes import resolve_frame, rotate_box

        geo = _Geometry()
        keys = set(self.faces_by_photo) | set(self.syno_by_photo)
        for key in keys:
            photo = self.photos.get(key)
            if photo is None or photo.deleted:
                continue
            space = key[0]
            fids = self.faces_by_photo.get(key, [])
            syn = self.syno_by_photo.get(key, [])
            if not syn:
                for fid in fids:
                    geo.faces[fid] = ("no_synology_faces", None, 0.0)
                continue
            if not fids and key not in self.extract_log:
                for sf in syn:
                    geo.syno[(space, sf.syno_face_id)] = ("not_detected", None, 0.0)
                continue

            W, H = photo.width, photo.height
            theirs = [(sf.syno_face_id, sf.box) for sf in syn]
            ours: list[tuple[int, tuple[float, float, float, float]]] = []
            if W and H:
                for fid in fids:
                    f = self.faces[fid]
                    ours.append((fid, (f.x / W, f.y / H, (f.x + f.w) / W, (f.y + f.h) / H)))
            pairs = match_boxes(ours, theirs) if ours else []
            matched_f = {fid: (sid, iou) for fid, sid, iou in pairs}
            matched_s = {sid: (fid, iou) for fid, sid, iou in pairs}
            un_s = [sf for sf in syn if sf.syno_face_id not in matched_s]
            un_f = [fid for fid in fids if fid not in matched_f]

            fixed_f: dict[int, tuple[int, float]] = {}
            fixed_s: dict[int, tuple[int, float]] = {}
            if un_s and un_f:
                # The same (frame, quarter-turn) search Inspect draws with. When
                # it re-seats our boxes, an unmatched pair is a geometry fault
                # (crossref still sees it unmatched), not a detection miss.
                photo_d = {"width": W, "height": H}
                face_d = [
                    {"x": self.faces[fid].x, "y": self.faces[fid].y,
                     "w": self.faces[fid].w, "h": self.faces[fid].h}
                    for fid in fids
                ]
                syno_d = [
                    {"x1": sf.box[0], "y1": sf.box[1], "x2": sf.box[2], "y2": sf.box[3]}
                    for sf in syn
                ]
                frame, turn, source = resolve_frame(photo_d, face_d, syno_d)
                seed = (float(W or 0), float(H or 0))
                if source == "synology-faces" and (tuple(frame) != seed or turn):
                    fw, fh = frame
                    ours2 = []
                    for fid in un_f:
                        f = self.faces[fid]
                        b = rotate_box(
                            {"x": f.x / fw, "y": f.y / fh, "w": f.w / fw, "h": f.h / fh}, turn
                        )
                        ours2.append((fid, (b["x"], b["y"], b["x"] + b["w"], b["y"] + b["h"])))
                    theirs2 = [(sf.syno_face_id, sf.box) for sf in un_s]
                    for fid, sid, iou in match_boxes(ours2, theirs2):
                        fixed_f[fid] = (sid, iou)
                        fixed_s[sid] = (fid, iou)
                    if fixed_f:
                        geo.corrected[key] = {
                            "frame": [_r(fw, 1), _r(fh, 1)],
                            "rotation": int(turn),
                            "cause": "no_resolution" if not (W and H) else (
                                "rotation" if turn else "transposed_resolution"
                            ),
                        }

            for fid in fids:
                if fid in matched_f:
                    sid, iou = matched_f[fid]
                    geo.faces[fid] = ("matched", sid, iou)
                elif fid in fixed_f:
                    sid, iou = fixed_f[fid]
                    geo.faces[fid] = ("frame_mismatch", sid, iou)
                else:
                    geo.faces[fid] = ("extra", None, 0.0)
            for sf in syn:
                sk = (space, sf.syno_face_id)
                if sf.syno_face_id in matched_s:
                    fid, iou = matched_s[sf.syno_face_id]
                    geo.syno[sk] = ("matched", fid, iou)
                elif sf.syno_face_id in fixed_s:
                    fid, iou = fixed_s[sf.syno_face_id]
                    geo.syno[sk] = ("frame_mismatch", fid, iou)
                else:
                    geo.syno[sk] = ("missed", None, 0.0)
                    best = max(
                        ((fid, _iou(box, sf.box)) for fid, box in ours),
                        key=lambda p: p[1],
                        default=None,
                    )
                    if best is not None and best[1] > 0:
                        geo.near_miss[sk] = best
        return geo


_LIBRARY = _Memo()
_FUSED = _Memo(idle_seconds=_IDLE_SECONDS)
_GRAPH = _Memo(idle_seconds=_IDLE_SECONDS)
_CENTROIDS = _Memo(idle_seconds=_IDLE_SECONDS)
_RUN = _Memo()


def _library(conn: Connection) -> tuple[_Library, tuple | None]:
    fp = _fingerprint(conn)
    lib_key = None if fp is None else fp[:-1]
    return _LIBRARY.get(lib_key, lambda: _Library(conn)), fp


@dataclass
class _Cluster:
    size: int
    mapped: PersonKey | None
    vote_fraction: float | None
    labeled_count: int | None
    cohesion: float | None = None


@dataclass
class _Run:
    run_id: int
    created_at: int
    params: dict
    is_latest: bool
    clusters: dict[int, _Cluster]
    member_of: dict[int, int]
    members: dict[int, list[int]]

    def clustering(self, key: str, default: Any = None) -> Any:
        return (self.params.get("clustering") or {}).get(key, default)

    def crossref(self, key: str, default: Any = None) -> Any:
        return (self.params.get("crossref") or {}).get(key, default)


def _run(conn: Connection, fp: tuple | None, run_id: int | None) -> _Run:
    latest_row = conn.execute("SELECT MAX(run_id) FROM cluster_runs").fetchone()
    latest = None if latest_row is None or latest_row[0] is None else int(latest_row[0])
    if latest is None:
        raise ToolError(
            "no face grouping run exists yet. Run Group faces (job 'cluster') after "
            "Detect faces, then call this again."
        )
    wanted = latest if run_id is None else int(run_id)

    def build() -> _Run:
        row = conn.execute(
            "SELECT run_id, params_json, created_at FROM cluster_runs WHERE run_id = ?",
            (wanted,),
        ).fetchone()
        if row is None:
            raise ToolError(f"no grouping run {wanted}; the latest is {latest}")
        try:
            params = json.loads(row["params_json"]) or {}
        except (TypeError, ValueError):
            params = {}
        clusters: dict[int, _Cluster] = {}
        for c in conn.execute(
            "SELECT cluster_id, size, mapped_person_id, map_space, vote_fraction, "
            "labeled_count, cohesion FROM clusters WHERE run_id = ?",
            (wanted,),
        ):
            mapped = (
                (str(c["map_space"]), int(c["mapped_person_id"]))
                if c["mapped_person_id"] is not None and c["map_space"]
                else None
            )
            clusters[int(c["cluster_id"])] = _Cluster(
                int(c["size"]),
                mapped,
                None if c["vote_fraction"] is None else float(c["vote_fraction"]),
                None if c["labeled_count"] is None else int(c["labeled_count"]),
                None if c["cohesion"] is None else float(c["cohesion"]),
            )
        member_of: dict[int, int] = {}
        members: dict[int, list[int]] = defaultdict(list)
        for m in conn.execute(
            "SELECT cluster_id, face_id FROM cluster_members WHERE run_id = ? ORDER BY face_id",
            (wanted,),
        ):
            cid, fid = int(m["cluster_id"]), int(m["face_id"])
            member_of[fid] = cid
            members[cid].append(fid)
        return _Run(
            wanted, int(row["created_at"]), params, wanted == latest, clusters,
            member_of, dict(members),
        )

    key = None if fp is None else (fp, wanted)
    return _RUN.get(key, build)


@dataclass
class _Fused:
    key: Any
    face_ids: np.ndarray
    X: np.ndarray
    pos_of: dict[int, int]
    models: list[str]
    weights: dict[str, float]


def _weights_for(run: _Run | None, tuning) -> dict[str, float]:
    if run is not None and isinstance(run.clustering("fusion_weights"), dict):
        return {str(k): float(v) for k, v in run.clustering("fusion_weights").items()}
    return {str(k): float(v) for k, v in tuning.clustering.fusion_weights.items()}


def _fused(ctx: ToolContext, conn: Connection, fp: tuple | None, weights: dict) -> _Fused:
    key = None if fp is None else (fp[:-1], tuple(sorted(weights.items())))

    def build() -> _Fused:
        from ...cluster import graph

        settings = ctx.settings.model_copy(
            update={
                "clustering": ctx.settings.clustering.model_copy(
                    update={"fusion_weights": dict(weights)}
                )
            }
        )
        face_ids, X = graph.load_fused(conn, settings)
        models = sorted(
            str(r[0])
            for r in conn.execute(
                "SELECT DISTINCT model FROM embeddings WHERE variant = 'orig'"
            )
        )
        return _Fused(
            key,
            face_ids,
            X,
            {int(f): i for i, f in enumerate(face_ids.tolist())},
            models,
            dict(weights),
        )

    return _FUSED.get(key, build)


def _graph(ctx: ToolContext, fused: _Fused, k: int) -> tuple[np.ndarray, np.ndarray] | None:
    """The kNN graph clustering cached for exactly these faces, k and weights."""

    def build():
        from ...cluster import graph

        try:
            return (graph.load_graph(ctx.settings.storage.data_dir, fused.face_ids, k, fused.weights),)
        except Exception:  # noqa: BLE001 - a corrupt cache file is just a miss
            log.debug("mcp diag: kNN graph cache unreadable", exc_info=True)
            return (None,)

    key = None if fused.key is None else (fused.key, int(k))
    return _GRAPH.get(key, build)[0]


@dataclass
class _Centroids:
    cluster_ids: list[int]
    index: dict[int, int]
    pos_cluster: np.ndarray  # cluster index per fused position, -1 outside the run
    norms: np.ndarray  # |sum of member vectors| per cluster


def _centroids(fused: _Fused, run: _Run, fp: tuple | None) -> _Centroids:
    def build() -> _Centroids:
        from scipy import sparse

        cids = sorted(run.members)
        index = {cid: i for i, cid in enumerate(cids)}
        n = fused.X.shape[0]
        pos_cluster = np.full(n, -1, dtype=np.int64)
        for fid, cid in run.member_of.items():
            pos = fused.pos_of.get(fid)
            if pos is not None:
                pos_cluster[pos] = index[cid]
        valid = np.nonzero(pos_cluster >= 0)[0]
        norms2 = np.zeros(len(cids), dtype=np.float64)
        if valid.size and n:
            M = sparse.csr_matrix(
                (np.ones(valid.size, dtype=np.float32), (pos_cluster[valid], valid)),
                shape=(len(cids), n),
            )
            # Column blocks keep the transient clusters x dims matrix small.
            for start in range(0, fused.X.shape[1], 128):
                S = np.asarray(M @ fused.X[:, start : start + 128], dtype=np.float64)
                norms2 += (S * S).sum(axis=1)
        return _Centroids(cids, index, pos_cluster, np.sqrt(norms2))

    key = None if fused.key is None or fp is None else (fused.key, fp[-1], run.run_id)
    return _CENTROIDS.get(key, build)


def _settings_block(tuning, *sections: str) -> dict:
    return {s: getattr(tuning, s).model_dump(mode="json") for s in sections}


def _changed_since_run(run: _Run, tuning) -> dict:
    out = {}
    for section in ("clustering", "crossref"):
        then = run.params.get(section) or {}
        now = getattr(tuning, section).model_dump(mode="json")
        for key, value in now.items():
            if key in then and then[key] != value:
                out[f"{section}.{key}"] = {"run": then[key], "now": value}
    return out


def _min_side(face: _Face) -> float:
    return min(face.w, face.h)


def _syno_min_side_px(sf: _Syno, photo: _Photo | None) -> float | None:
    if photo is None or not photo.width or not photo.height:
        return None
    return min((sf.box[2] - sf.box[0]) * photo.width, (sf.box[3] - sf.box[1]) * photo.height)


def _face_brief(lib: _Library, fid: int, labels: dict[int, PersonKey] | None = None) -> dict:
    f = lib.faces[fid]
    out = {
        "face_id": fid,
        "space": f.space,
        "photo_id": f.photo_id,
        "detector": f.detector,
        "det_score": _r(f.det_score, 3),
        "quality": _r(f.quality, 2),
        "min_side_px": _r(_min_side(f), 1),
    }
    if labels is not None:
        out["synology_person"] = lib.person(labels.get(fid))
    return out


def _cluster_brief(lib: _Library, run: _Run, cid: int) -> dict:
    c = run.clusters.get(cid)
    if c is None:
        return {"cluster_id": cid}
    return {
        "cluster_id": cid,
        "size": c.size,
        "mapped_person": lib.person(c.mapped),
        "vote_fraction": _r(c.vote_fraction, 3),
        "cohesion": _r(c.cohesion, 4),
    }


# --------------------------------------------------------------------------- #
# Detection                                                                     #
# --------------------------------------------------------------------------- #
@tool(
    "detection_summary",
    "How well Detect faces is covering the library and how it agrees with "
    "Synology's own face boxes. Returns photo coverage (detected at the current "
    "pipeline version, pending, skipped as unreadable by reason), face counts per "
    "detector (scrfd = main detector only, yolo = secondary detector only and "
    "unconfirmed, merged = both agreed), histograms of detection score, face size "
    "(shorter box side in px) and quality, photos by face count, agreement with "
    "Synology (its boxes we matched, missed, or matched only after re-seating a "
    "rotated frame; our boxes it has none for), and the [detection] settings in "
    "force. Start here for any 'faces are missing / wrong boxes' question, then "
    "drill in with find_detection_gaps, inspect_photo and render_photo.",
    properties={
        "space": {"type": "string", "enum": list(_SPACES), "description": "Limit to one space."},
    },
)
def detection_summary(ctx: ToolContext, args: dict) -> dict:
    space = _space_arg(ctx, args)
    tuning = _tuning(ctx)
    version = _pipeline_version(ctx, tuning)
    with _db(ctx) as conn:
        lib, _ = _library(conn)
        geo = lib.geometry()

    def in_scope(s: str) -> bool:
        return space is None or s == space

    photos = Counter()
    skip_reasons: Counter = Counter()
    by_face_count: Counter = Counter()
    for key, photo in lib.photos.items():
        if not in_scope(key[0]) or photo.deleted:
            continue
        photos["synced"] += 1
        if photo.type not in ("photo", "live"):
            photos["not_a_photo"] += 1
            continue
        photos["eligible"] += 1
        entry = lib.extract_log.get(key)
        skip = lib.skips.get(key)
        skip_applies = (
            skip is not None
            and (version is None or skip[1] == version)
            and skip[0] == photo.cache_key
        )
        if entry is not None:
            count = entry[2]
            by_face_count["10+" if count >= 10 else "5-9" if count >= 5 else str(count)] += 1
        if entry is not None and (version is None or entry[1] == version) and entry[0] == photo.cache_key:
            photos["detected_current"] += 1
        elif skip_applies:
            photos["skipped_unreadable"] += 1
            skip_reasons[skip[2]] += 1
        elif entry is None:
            photos["never_detected"] += 1
        elif version is not None and entry[1] != version:
            photos["older_pipeline_version"] += 1
        else:
            photos["file_changed_since_detection"] += 1

    det_counts: Counter = Counter()
    scores: list[float] = []
    sizes: list[float] = []
    qualities: list[float] = []
    stale_faces = no_crop = below_min = 0
    min_px = tuning.detection.min_face_px
    for fid, f in lib.faces.items():
        if not in_scope(f.space) or not lib.face_live(fid):
            continue
        det_counts[f.detector] += 1
        if f.det_score is not None:
            scores.append(f.det_score)
        sizes.append(_min_side(f))
        if f.quality is not None:
            qualities.append(f.quality)
        if version is not None and f.pipeline_version != version:
            stale_faces += 1
        if not f.has_crop:
            no_crop += 1
        if _min_side(f) < min_px:
            below_min += 1

    syno_counts: Counter = Counter()
    missed_px: list[float] = []
    missed_listed = 0
    for (s, sid), (verdict, _, _) in geo.syno.items():
        if not in_scope(s):
            continue
        syno_counts[verdict] += 1
    for key, boxes in lib.syno_by_photo.items():
        if not in_scope(key[0]) or not lib.photo_live(key):
            continue
        for sf in boxes:
            if geo.syno.get((key[0], sf.syno_face_id), ("",))[0] != "missed":
                continue
            px = _syno_min_side_px(sf, lib.photos.get(key))
            if px is not None:
                missed_px.append(px)
            if sf.person_id is not None and (key[0], sf.person_id) in lib.known:
                missed_listed += 1
    face_counts: Counter = Counter()
    extra_by_detector: Counter = Counter()
    for fid, (verdict, _, _) in geo.faces.items():
        f = lib.faces[fid]
        if not in_scope(f.space):
            continue
        face_counts[verdict] += 1
        if verdict == "extra":
            extra_by_detector[f.detector] += 1
    checkable = syno_counts["matched"] + syno_counts["missed"] + syno_counts["frame_mismatch"]

    return {
        "scope": {"space": space or "all"},
        "pipeline_version": version,
        "photos": {
            "synced": photos["synced"],
            "not_a_photo": photos["not_a_photo"],
            "eligible": photos["eligible"],
            "detected_current": photos["detected_current"],
            "pending": {
                "never_detected": photos["never_detected"],
                "older_pipeline_version": photos["older_pipeline_version"],
                "file_changed_since_detection": photos["file_changed_since_detection"],
            },
            "skipped_unreadable": {
                "total": photos["skipped_unreadable"],
                "by_reason": dict(skip_reasons.most_common()),
            },
            "by_face_count": dict(sorted(by_face_count.items())),
        },
        "faces": {
            "total": sum(det_counts.values()),
            "by_detector": dict(det_counts.most_common()),
            "from_older_pipeline_version": stale_faces if version is not None else None,
            "without_crop": no_crop,
            "below_current_min_face_px": below_min,
            "det_score": {"quantiles": _quantiles(scores), "histogram": _hist(scores, _SCORE_EDGES)},
            "min_side_px": {"quantiles": _quantiles(sizes, 1), "histogram": _hist(sizes, _PX_EDGES)},
            "quality": {"quantiles": _quantiles(qualities, 2)},
        },
        "synology_agreement": {
            "synology_boxes_on_detected_photos": checkable,
            "matched": syno_counts["matched"],
            "matched_after_frame_correction": syno_counts["frame_mismatch"],
            "missed": syno_counts["missed"],
            "missed_on_listed_persons": missed_listed,
            "on_photos_not_yet_detected": syno_counts["not_detected"],
            "recall_vs_synology": _r(syno_counts["matched"] / checkable, 4) if checkable else None,
            "missed_box_min_side_px": {
                "quantiles": _quantiles(missed_px, 1),
                "histogram": _hist(missed_px, _PX_EDGES),
            },
            "our_faces_matched": face_counts["matched"],
            "our_faces_unmatched_on_photos_with_synology_boxes": face_counts["extra"],
            "unmatched_by_detector": dict(extra_by_detector.most_common()),
            "our_faces_on_photos_without_synology_boxes": face_counts["no_synology_faces"],
            "photos_with_frame_correction": sum(
                1 for k in geo.corrected if in_scope(k[0])
            ),
        },
        "settings": _settings_block(tuning, "detection"),
        "notes": [
            "Pairing is IoU >= 0.3 on boxes normalised by the photo's stored resolution, "
            "exactly what face grouping uses to read Synology's labels.",
            "matched_after_frame_correction: boxes that only line up after Inspect's "
            "frame/rotation search; grouping still sees them as unlabelled.",
            "Synology boxes cover only faces Synology found; an unmatched face of ours "
            "is a false positive or a face Synology missed. Look before concluding.",
            "Detection settings changes alter the pipeline version: every photo is "
            "re-detected on the next Detect faces run.",
        ],
        "next_steps": [
            "find_detection_gaps kind=missed (largest Synology boxes we did not find)",
            "find_detection_gaps kind=extra / low_score / small_faces",
            "list_extract_skips for unreadable photos",
            "render_photo on an example to see both sets of boxes",
        ],
    }


_GAP_KINDS = ("missed", "frame_mismatch", "extra", "low_score", "small_faces", "skipped")


@tool(
    "find_detection_gaps",
    "Lists concrete examples of one kind of detection problem, worst first, with "
    "ids to follow up. kinds: 'missed' = a Synology face box on a photo we "
    "detected, with none of our boxes on it (largest first; near_miss_iou > 0 "
    "means a box of ours overlaps but too little, i.e. a misplaced box rather than "
    "no detection); 'frame_mismatch' = boxes that only line up after re-seating a "
    "rotated or transposed frame (a geometry fault, not a detection miss); "
    "'extra' = our faces matching no Synology box on photos where Synology has "
    "boxes (lowest score first; false positive or a face Synology missed); "
    "'low_score' = faces below max_score; 'small_faces' = faces whose shorter side "
    "is below max_px; 'skipped' = photos detection could not read. Follow up with "
    "render_photo / inspect_photo (space, photo_id) or get_face_crops (face_id).",
    properties={
        "kind": {"type": "string", "enum": list(_GAP_KINDS)},
        "space": {"type": "string", "enum": list(_SPACES)},
        "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_LIMIT, "default": _DEFAULT_LIMIT},
        "offset": {"type": "integer", "minimum": 0, "default": 0},
        "max_score": {"type": "number", "description": "low_score: detection score cutoff (default 0.6)."},
        "max_px": {"type": "number", "description": "small_faces: shorter-side cutoff in px (default 2 x min_face_px)."},
        "only_photos_with_synology_boxes": {
            "type": "boolean",
            "default": True,
            "description": "extra: skip photos Synology has no boxes on (nothing to compare).",
        },
    },
    required=["kind"],
)
def find_detection_gaps(ctx: ToolContext, args: dict) -> dict:
    kind = arg_str(args, "kind", choices=_GAP_KINDS)
    if kind is None:
        raise ToolError("'kind' is required")
    space = _space_arg(ctx, args)
    limit = _limit(args)
    offset = int(arg_int(args, "offset", 0, lo=0))
    if kind == "skipped":
        return _skips(ctx, space=space, reason=None, limit=limit, offset=offset, include_stale=False)
    tuning = _tuning(ctx)
    with _db(ctx) as conn:
        lib, _ = _library(conn)
        geo = lib.geometry()
        labels = lib.labels(conn, ctx.settings)[0] if kind in ("low_score", "small_faces") else {}

    items: list[tuple[float, dict]] = []
    if kind in ("missed", "frame_mismatch"):
        for key, boxes in lib.syno_by_photo.items():
            if (space and key[0] != space) or not lib.photo_live(key):
                continue
            photo = lib.photos[key]
            for sf in boxes:
                verdict, fid, iou = geo.syno.get((key[0], sf.syno_face_id), ("", None, 0.0))
                if verdict != kind:
                    continue
                px = _syno_min_side_px(sf, photo)
                pk = (key[0], sf.person_id) if sf.person_id is not None else None
                item = {
                    "space": key[0],
                    "photo_id": key[1],
                    "filename": photo.filename,
                    "syno_face_id": sf.syno_face_id,
                    "synology_person": lib.person(pk),
                    "box": [_r(v, 4) for v in sf.box],
                    "box_min_side_px": _r(px, 1),
                    "our_faces_on_photo": len(lib.faces_by_photo.get(key, [])),
                }
                if kind == "missed":
                    near = geo.near_miss.get((key[0], sf.syno_face_id))
                    item["near_miss_face_id"] = near[0] if near else None
                    item["near_miss_iou"] = _r(near[1], 3) if near else 0.0
                    entry = lib.extract_log.get(key)
                    item["detected_with_pipeline_version"] = entry[1] if entry else None
                else:
                    item["matched_face_id"] = fid
                    item["iou_after_correction"] = _r(iou, 3)
                    item["correction"] = geo.corrected.get(key)
                items.append((-(px or 0.0), item))
    else:
        max_score = _arg_float(args, "max_score", 0.6) if kind == "low_score" else None
        max_px = (
            _arg_float(args, "max_px", 2.0 * tuning.detection.min_face_px)
            if kind == "small_faces"
            else None
        )
        only_syno = arg_bool(args, "only_photos_with_synology_boxes", True)
        for fid, f in lib.faces.items():
            if (space and f.space != space) or not lib.face_live(fid):
                continue
            verdict, sid, iou = geo.faces.get(fid, ("", None, 0.0))
            if kind == "extra":
                if verdict == "no_synology_faces" and not only_syno:
                    pass
                elif verdict != "extra":
                    continue
                sort = f.det_score if f.det_score is not None else 0.0
            elif kind == "low_score":
                if f.det_score is None or f.det_score >= max_score:
                    continue
                sort = f.det_score
            else:
                if _min_side(f) >= max_px:
                    continue
                sort = _min_side(f)
            item = _face_brief(lib, fid, labels if labels else None)
            item["det_score_secondary"] = _r(f.det_score_secondary, 3)
            item["synology_match"] = verdict
            if kind == "extra":
                item["photo_has_synology_boxes"] = verdict != "no_synology_faces"
            items.append((sort, item))

    items.sort(key=lambda p: p[0])
    page = [it for _, it in items[offset : offset + limit]]
    out: dict[str, Any] = {
        "kind": kind,
        "total": len(items),
        "offset": offset,
        "items": page,
        "settings": {
            "min_face_px": tuning.detection.min_face_px,
            "scrfd_score": tuning.detection.scrfd_score,
            "yolo_score": tuning.detection.yolo_score,
            "scales": list(tuning.detection.scales),
        },
    }
    if kind == "low_score":
        out["max_score"] = max_score
    if kind == "small_faces":
        out["max_px"] = max_px
    return out


@tool(
    "list_extract_skips",
    "Photos Detect faces could not read (an undecodable or oversized file) and so "
    "leaves out of later runs until the file or the pipeline version changes. "
    "Returns counts by reason and the photos, newest first. still_applies=false "
    "means the row no longer suppresses the photo. A human can retry them with "
    "the 'extract' job's retry-skipped option.",
    properties={
        "reason": {"type": "string", "description": "Only this reason (exact text)."},
        "space": {"type": "string", "enum": list(_SPACES)},
        "include_stale": {"type": "boolean", "default": False},
        "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_LIMIT, "default": _DEFAULT_LIMIT},
        "offset": {"type": "integer", "minimum": 0, "default": 0},
    },
)
def list_extract_skips(ctx: ToolContext, args: dict) -> dict:
    return _skips(
        ctx,
        space=_space_arg(ctx, args),
        reason=arg_str(args, "reason"),
        limit=_limit(args),
        offset=int(arg_int(args, "offset", 0, lo=0)),
        include_stale=arg_bool(args, "include_stale", False),
    )


def _skips(ctx, *, space, reason, limit, offset, include_stale) -> dict:
    version = _pipeline_version(ctx, _tuning(ctx))
    with _db(ctx) as conn:
        lib, _ = _library(conn)
    rows = []
    by_reason: Counter = Counter()
    for key, (cache_key, pv, why, at) in lib.skips.items():
        photo = lib.photos.get(key)
        if photo is None or photo.deleted or (space and key[0] != space):
            continue
        applies = cache_key == photo.cache_key and (version is None or pv == version)
        if applies:
            by_reason[why] += 1
        if (not applies and not include_stale) or (reason and why != reason):
            continue
        rows.append(
            {
                "space": key[0],
                "photo_id": key[1],
                "filename": photo.filename,
                "reason": why,
                "skipped_at": at,
                "still_applies": applies,
            }
        )
    rows.sort(key=lambda r: (-r["skipped_at"], r["photo_id"]))
    return {
        "kind": "skipped",
        "total": len(rows),
        "offset": offset,
        "by_reason": dict(by_reason.most_common()),
        "items": rows[offset : offset + limit],
    }


@tool(
    "inspect_photo",
    "Everything the database knows about one photo: our detections (pixel box, "
    "box normalised to the display frame, detector, scores, quality, size, crop "
    "present, cluster and its mapped person) next to Synology's boxes (person, "
    "listed or not, which of our faces matches it), the Detect faces log row, "
    "whether the photo is skipped, and the review items about it. The display "
    "block's rotation says how our boxes turn onto Synology's frame. Use after "
    "find_detection_gaps or explain_face; render_photo draws the same boxes.",
    properties={
        "space": {"type": "string", "enum": list(_SPACES)},
        "photo_id": {"type": "integer"},
    },
    required=["photo_id"],
)
def inspect_photo(ctx: ToolContext, args: dict) -> dict:
    from ..inspect_routes import photo_report

    space = _space_arg(ctx, args, required=True)
    photo_id = int(arg_int(args, "photo_id"))
    tuning = _tuning(ctx)
    version = _pipeline_version(ctx, tuning)
    with _db(ctx) as conn:
        report = photo_report(conn, ctx.settings, space, photo_id)
        if report is None:
            raise ToolError(f"photo {space}/{photo_id} is not in the local library")
        lib, _ = _library(conn)
        voided = _voided(ctx, conn)

    matched_syno: dict[int, int] = {}
    faces = []
    for f in report["faces"]:
        syno = f.get("syno")
        if syno is not None:
            matched_syno[int(syno["syno_face_id"])] = f["face_id"]
        cluster = f.get("cluster")
        bbox = f["bbox"]
        faces.append(
            {
                "face_id": f["face_id"],
                "detector": f["detector"],
                "bbox_px": [_r(bbox["x"], 1), _r(bbox["y"], 1), _r(bbox["w"], 1), _r(bbox["h"], 1)],
                "box": (
                    None
                    if f["box"] is None
                    else [_r(f["box"][k], 4) for k in ("x", "y", "w", "h")]
                ),
                "min_side_px": _r(min(bbox["w"], bbox["h"]), 1),
                "det_score": _r(f["det_score"], 3),
                "det_score_secondary": _r(f["det_score_secondary"], 3),
                "quality": _r(f["quality"], 2),
                "restored": f["restored"],
                "pipeline_version": f["pipeline_version"],
                "current_pipeline_version": (
                    None if version is None else f["pipeline_version"] == version
                ),
                "has_crop": bool(f["crop_url"]),
                "embedding_models": sorted(
                    e["model"] for e in f["embeddings"] if e["variant"] == "orig"
                ),
                "cluster": (
                    None
                    if cluster is None
                    else {
                        "run_id": cluster["run_id"],
                        "cluster_id": cluster["cluster_id"],
                        "size": cluster["size"],
                        "mapped_person": lib.person(
                            (str(cluster["map_space"]), int(cluster["mapped_person_id"]))
                            if cluster["mapped_person_id"] is not None and cluster["map_space"]
                            else None
                        ),
                        "vote_fraction": _r(cluster["vote_fraction"], 3),
                    }
                ),
                "synology_match": (
                    None
                    if syno is None
                    else {
                        "syno_face_id": syno["syno_face_id"],
                        "person": lib.person(
                            (space, int(syno["person_id"])) if syno["person_id"] is not None else None
                        ),
                        "iou": _r(syno["iou"], 3),
                    }
                ),
            }
        )

    photo_meta = lib.photos.get((space, photo_id))
    syno_faces = []
    for sf in report["syno_faces"]:
        box = sf["box"]
        syno_faces.append(
            {
                "syno_face_id": sf["syno_face_id"],
                "person": lib.person(
                    (space, int(sf["person_id"])) if sf["person_id"] is not None else None
                ),
                "box": [_r(box[k], 4) for k in ("x", "y", "w", "h")],
                "min_side_px": (
                    _r(min(box["w"] * photo_meta.width, box["h"] * photo_meta.height), 1)
                    if photo_meta and photo_meta.width and photo_meta.height
                    else None
                ),
                "matched_face_id": matched_syno.get(int(sf["syno_face_id"])),
            }
        )

    review = []
    for item in report["review_items"][:20]:
        payload = item["payload"] or {}
        review.append(
            {
                "item_id": item["item_id"],
                "kind": item["kind"],
                "status": item["status"],
                "confidence": _r(item["confidence"], 3),
                "face_ids": item["face_ids"],
                "person_id": payload.get("person_id"),
                "person_name": payload.get("person_name"),
                "voided": item["item_id"] in voided,
            }
        )

    skip = lib.skips.get((space, photo_id))
    extract = report["extract"]
    return {
        "space": space,
        "photo_id": photo_id,
        "photo": {
            k: report["photo"][k]
            for k in ("filename", "type", "width", "height", "orientation", "time", "deleted")
        },
        "display": report["display"],
        "detect_faces": (
            None
            if extract is None
            else {
                "pipeline_version": extract["pipeline_version"],
                "is_current_version": (
                    None if version is None else extract["pipeline_version"] == version
                ),
                "file_changed_since": extract["stale"],
                "face_count": extract["face_count"],
                "processed_at": extract["processed_at"],
            }
        ),
        "skipped_unreadable": (
            None if skip is None else {"reason": skip[2], "skipped_at": skip[3]}
        ),
        "faces": faces,
        "synology_faces": syno_faces,
        "synology_faces_unmatched": sum(1 for s in syno_faces if s["matched_face_id"] is None),
        "review_items": review,
        "review_items_total": len(report["review_items"]),
        "settings": {
            "min_face_px": tuning.detection.min_face_px,
            "scrfd_score": tuning.detection.scrfd_score,
            "yolo_score": tuning.detection.yolo_score,
        },
        "inspect_path": f"/inspect/{space}/{photo_id}",
    }


_OURS_MATCHED = (40, 220, 40)
_OURS_UNMATCHED = (255, 140, 0)
_THEIRS = (0, 170, 255)


@tool(
    "render_photo",
    "The photo itself (Synology's thumbnail, fetched from the NAS) with our "
    "detections drawn as boxes labelled '#face_id score' (green when a Synology box "
    "matches, orange when none does) and Synology's boxes in blue labelled with "
    "the person's name or id. Geometry is reconciled the way the Inspect page does "
    "it. Use it to see whether a 'missed' face is really a face, whether an "
    "'extra' box is a false positive, or why two boxes did not pair. Needs the web "
    "process's NAS connection.",
    properties={
        "space": {"type": "string", "enum": list(_SPACES)},
        "photo_id": {"type": "integer"},
        "max_side": {"type": "integer", "minimum": 256, "maximum": 2048, "default": 1024},
        "show": {"type": "string", "enum": ["both", "ours", "synology"], "default": "both"},
    },
    required=["photo_id"],
)
def render_photo(ctx: ToolContext, args: dict) -> ToolOutput:
    from ..inspect_routes import photo_report, rotate_box

    space = _space_arg(ctx, args, required=True)
    photo_id = int(arg_int(args, "photo_id"))
    max_side = int(arg_int(args, "max_side", 1024, lo=256, hi=2048))
    show = arg_str(args, "show", "both", choices=("both", "ours", "synology"))
    if ctx.nas_session is None:
        raise ToolError(
            "no NAS connection is available to fetch the photo. inspect_photo returns "
            "the same boxes as numbers."
        )
    with _db(ctx) as conn:
        report = photo_report(conn, ctx.settings, space, photo_id)
        if report is None:
            raise ToolError(f"photo {space}/{photo_id} is not in the local library")
        cache_key = (
            conn.execute(
                "SELECT cache_key FROM photos WHERE space = ? AND id = ?", (space, photo_id)
            ).fetchone()["cache_key"]
            or ""
        )

    data, source = _fetch_photo(ctx, space, photo_id, cache_key)

    from PIL import Image, ImageDraw, ImageFont, ImageOps

    try:
        img = Image.open(io.BytesIO(data))
        if source == "cached original":
            img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")
    except Exception as exc:  # noqa: BLE001 - any decoder failure
        raise ToolError(f"the photo could not be decoded ({type(exc).__name__})") from exc
    img.thumbnail((max_side, max_side))
    W, H = img.size
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(size=max(12, W // 60))
    except TypeError:
        font = ImageFont.load_default()
    stroke = max(2, W // 400)
    rotation = int(report["display"]["rotation"] or 0)

    def label(xy: tuple[float, float], text: str, colour, above: bool) -> None:
        x, y = xy
        bbox = draw.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        ty = y - th - 4 if above else y + 2
        ty = min(max(ty, 0), H - th - 4)
        tx = min(max(x, 0), W - tw - 4)
        draw.rectangle([tx, ty, tx + tw + 4, ty + th + 4], fill=colour)
        draw.text((tx + 2, ty + 1 - bbox[1]), text, fill=(0, 0, 0), font=font)

    legend = {"ours": [], "synology": []}
    if show in ("both", "synology"):
        for sf in report["syno_faces"]:
            b = sf["box"]
            x1, y1 = b["x"] * W, b["y"] * H
            x2, y2 = (b["x"] + b["w"]) * W, (b["y"] + b["h"]) * H
            draw.rectangle([x1, y1, x2, y2], outline=_THEIRS, width=max(1, stroke - 1))
            text = sf["name"] or (f"person {sf['person_id']}" if sf["person_id"] else "unnamed")
            label((x1, y2), text, _THEIRS, above=False)
            legend["synology"].append(
                {"syno_face_id": sf["syno_face_id"], "person_id": sf["person_id"], "label": text}
            )
    if show in ("both", "ours"):
        for f in report["faces"]:
            if f["box"] is None:
                continue
            b = rotate_box(f["box"], rotation)
            x1, y1 = b["x"] * W, b["y"] * H
            x2, y2 = (b["x"] + b["w"]) * W, (b["y"] + b["h"]) * H
            colour = _OURS_MATCHED if f.get("syno") else _OURS_UNMATCHED
            draw.rectangle([x1, y1, x2, y2], outline=colour, width=stroke)
            score = f["det_score"]
            text = f"#{f['face_id']}" + (f" {score:.2f}" if score is not None else "")
            label((x1, y1), text, colour, above=True)
            legend["ours"].append(
                {
                    "face_id": f["face_id"],
                    "detector": f["detector"],
                    "det_score": _r(score, 3),
                    "matches_synology_box": bool(f.get("syno")),
                }
            )

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    caption = (
        f"{space}/{photo_id} ({report['photo']['filename'] or 'unnamed'}), "
        f"{len(legend['ours'])} of our boxes (green matched, orange unmatched), "
        f"{len(legend['synology'])} Synology boxes (blue)"
    )
    return ToolOutput(
        structured={
            "space": space,
            "photo_id": photo_id,
            "image_size": [W, H],
            "image_source": source,
            "rotation_applied_to_our_boxes": rotation,
            "rotation_source": report["display"]["rotation_source"],
            "legend": legend,
        },
        images=[ImageContent(buf.getvalue(), "image/jpeg", caption)],
    )


def _fetch_photo(ctx: ToolContext, space: str, photo_id: int, cache_key: str) -> tuple[bytes, str]:
    from ...syno import foto
    from ...syno.client import SynoError

    try:
        with ctx.nas_session.use() as client:
            data = b"".join(
                foto.download_item_thumbnail(client, space, photo_id, cache_key, "xl")
            )
        return data, "synology thumbnail"
    except SynoError as exc:
        from ...sync.downloads import original_path

        with _db(ctx) as conn:
            row = conn.execute(
                "SELECT * FROM photos WHERE space = ? AND id = ?", (space, photo_id)
            ).fetchone()
        cached = original_path(ctx.settings, row) if row is not None else None
        if cached is not None and cached.is_file():
            try:
                import pillow_heif  # type: ignore

                pillow_heif.register_heif_opener()
            except Exception:  # noqa: BLE001 - only HEIC needs it
                pass
            return cached.read_bytes(), "cached original"
        raise ToolError(f"the NAS refused the thumbnail: {exc}") from exc


# --------------------------------------------------------------------------- #
# Grouping                                                                      #
# --------------------------------------------------------------------------- #
def _run_arg(args: dict) -> int | None:
    return arg_int(args, "run_id", None, lo=1)


@dataclass
class _RunLabels:
    """Crossref's labels restricted to live faces of one run."""

    labels: dict[int, PersonKey]
    votes: dict[int, Counter]  # cluster -> person -> faces
    by_person: dict[PersonKey, Counter]  # person -> cluster -> faces
    not_in_run: Counter  # person -> labelled live faces the run did not cluster
    samples: dict[tuple[int, PersonKey], list[int]]  # (cluster, person) -> a few face ids


def _run_labels(lib: _Library, run: _Run, labels: dict[int, PersonKey]) -> _RunLabels:
    votes: dict[int, Counter] = defaultdict(Counter)
    by_person: dict[PersonKey, Counter] = defaultdict(Counter)
    not_in_run: Counter = Counter()
    samples: dict[tuple[int, PersonKey], list[int]] = defaultdict(list)
    live = {}
    for fid in sorted(labels):
        pk = labels[fid]
        if not lib.face_live(fid):
            continue
        live[fid] = pk
        cid = run.member_of.get(fid)
        if cid is None:
            not_in_run[pk] += 1
            continue
        votes[cid][pk] += 1
        by_person[pk][cid] += 1
        if len(samples[(cid, pk)]) < 4:
            samples[(cid, pk)].append(fid)
    return _RunLabels(live, votes, by_person, not_in_run, samples)


def _why_unmapped(run: _Run, lib: _Library, votes: Counter, c: _Cluster | None = None) -> str:
    min_labeled = int(run.crossref("min_labeled", 3))
    majority = float(run.crossref("majority", 0.6))
    min_cohesion = run.crossref("min_cohesion")
    if not votes:
        return "no face in it carries a Synology label"
    listed = {pk: n for pk, n in votes.items() if pk in lib.known}
    if not listed:
        return "only Synology's unlisted persons label it; they are never a target (ADR 20)"
    total = sum(listed.values())
    if total < min_labeled:
        return f"{total} labelled face(s) of listed persons, below min_labeled={min_labeled}"
    top = max(listed.values())
    if top / total < majority:
        return f"top listed person holds {top}/{total} = {top / total:.2f}, below majority={majority}"
    if (
        c is not None
        and c.cohesion is not None
        and min_cohesion is not None
        and c.cohesion < float(min_cohesion)
    ):
        return (
            f"has a majority, but its faces are too unlike each other to be one person: "
            f"cohesion {c.cohesion:.3f}, below min_cohesion={min_cohesion}"
        )
    return "would map under the run's settings; the labels changed since the run"


def _breakdown(lib: _Library, votes: Counter, rl: _RunLabels, cid: int, limit: int = 6) -> list[dict]:
    return [
        {**lib.person(pk), "faces": n, "sample_face_ids": rl.samples.get((cid, pk), [])}
        for pk, n in sorted(votes.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    ]


@tool(
    "grouping_summary",
    "The shape of a Group faces run (the latest unless run_id is given): the "
    "settings it ran with and which have changed since, cluster size distribution, "
    "one-face groups, how many groups map to a Synology person and how firmly "
    "(vote_fraction) and how alike their faces are (cohesion, judged against "
    "min_cohesion), label-based quality (label_purity: share of labelled faces "
    "agreeing with their group's majority; label_completeness: share of a "
    "person's labelled faces in their largest group), faces detected since the "
    "run, review items it produced, and the [clustering]/[crossref] settings in "
    "force. Start here for 'people are split / merged' questions, then use "
    "find_grouping_issues, get_cluster, get_person and explain_face.",
    properties={"run_id": {"type": "integer", "minimum": 1}},
)
def grouping_summary(ctx: ToolContext, args: dict) -> dict:
    tuning = _tuning(ctx)
    with _db(ctx) as conn:
        lib, fp = _library(conn)
        run = _run(conn, fp, _run_arg(args))
        labels = lib.labels(conn, ctx.settings)[0]
        voided = _voided(ctx, conn)
        review_rows = conn.execute(
            "SELECT item_id, kind, status FROM review_queue WHERE run_id = ?", (run.run_id,)
        ).fetchall()
        unclustered = conn.execute(
            "SELECT COUNT(DISTINCT e.face_id) FROM embeddings e "
            "WHERE e.variant = 'orig' AND NOT EXISTS ("
            "  SELECT 1 FROM cluster_members cm WHERE cm.run_id = ? AND cm.face_id = e.face_id) "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM faces f JOIN photos p ON p.space = f.space AND p.id = f.photo_id "
            "  WHERE f.face_id = e.face_id AND p.deleted = 1)",
            (run.run_id,),
        ).fetchone()[0]
        run_count = conn.execute("SELECT COUNT(*) FROM cluster_runs").fetchone()[0]

    rl = _run_labels(lib, run, labels)
    buckets = []
    for lo, hi in _SIZE_BUCKETS:
        sizes = [c.size for c in run.clusters.values() if c.size >= lo and (hi is None or c.size <= hi)]
        buckets.append(
            {"size": _size_bucket_label(lo, hi), "clusters": len(sizes), "faces": sum(sizes)}
        )
    total_faces = sum(c.size for c in run.clusters.values())
    singletons = sum(1 for c in run.clusters.values() if c.size == 1)
    mapped = [c for c in run.clusters.values() if c.mapped is not None]
    mapped_faces = sum(c.size for c in mapped)
    new_min = int(run.crossref("new_person_min_faces", 5))
    unmapped_big = [
        c for c in run.clusters.values() if c.mapped is None and c.size >= new_min
    ]
    vfs = [c.vote_fraction for c in mapped if c.vote_fraction is not None]
    largest = sorted(run.clusters.items(), key=lambda kv: (-kv[1].size, kv[0]))[:10]

    labelled = sum(sum(v.values()) for v in rl.votes.values())
    agreeing = sum(max(v.values()) for v in rl.votes.values() if v)
    completeness = sum(max(c.values()) for c in rl.by_person.values() if c)
    split_persons = sum(1 for c in rl.by_person.values() if len(c) > 1)
    mixed = sum(1 for v in rl.votes.values() if len(v) > 1)
    listed_labelled = sum(
        n for v in rl.votes.values() for pk, n in v.items() if pk in lib.known
    )

    review: dict[str, Counter] = defaultdict(Counter)
    for row in review_rows:
        if int(row["item_id"]) in voided:
            continue
        review[str(row["kind"])][str(row["status"])] += 1

    return {
        "run": {
            "run_id": run.run_id,
            "created_at": run.created_at,
            "is_latest": run.is_latest,
            "runs_total": int(run_count),
            "params": run.params,
            "changed_since_run": _changed_since_run(run, tuning),
        },
        "faces": {
            "in_run": total_faces,
            "detected_since_run_or_left_out": int(unclustered or 0),
        },
        "clusters": {
            "total": len(run.clusters),
            "by_size": buckets,
            "singletons": singletons,
            "singleton_face_share": _r(singletons / total_faces, 4) if total_faces else None,
            "largest": [_cluster_brief(lib, run, cid) for cid, _ in largest],
        },
        "mapping": {
            "mapped_clusters": len(mapped),
            "mapped_face_share": _r(mapped_faces / total_faces, 4) if total_faces else None,
            "mapped_to_unlisted_person": sum(1 for c in mapped if c.mapped not in lib.known),
            "unmapped_clusters_at_least_new_person_min_faces": len(unmapped_big),
            "vote_fraction": {"quantiles": _quantiles(vfs), "histogram": _hist(vfs, _VOTE_EDGES)},
            "cohesion": _quantiles([c.cohesion for c in mapped if c.cohesion is not None], 4),
        },
        "labels": {
            "labelled_faces_in_run": labelled,
            "of_listed_persons": listed_labelled,
            "persons": len(rl.by_person),
            "label_purity": _r(agreeing / labelled, 4) if labelled else None,
            "label_completeness": _r(completeness / labelled, 4) if labelled else None,
            "persons_split_over_several_clusters": split_persons,
            "clusters_with_several_persons": mixed,
        },
        "review_items_from_run": {k: dict(v) for k, v in sorted(review.items())},
        "settings": _settings_block(tuning, "clustering", "crossref"),
        "notes": [
            "Labels are Synology's, read through IoU box matching (and a one-face "
            "photo fallback). Unlisted persons are Synology's hidden one- and "
            "two-face persons: they count as labels but never as a mapping target.",
            "label_purity falls when groups merge different people (edge_threshold "
            "too low, knn_k too high); label_completeness falls when one person is "
            "split (edge_threshold too high, knn_k too low, small or blurry faces).",
            "edge_threshold, algorithm, cw_iterations and min_cluster_size only need "
            "the 'recluster' job; knn_k and fusion_weights rebuild the kNN graph.",
        ],
        "next_steps": [
            "find_grouping_issues kind=split_persons / mixed_clusters / large_unmapped / low_vote",
            "get_cluster cluster_id=<id> for one group",
            "explain_face face_id=<id> for why a face sits where it does",
        ],
    }


_ISSUE_KINDS = ("split_persons", "mixed_clusters", "large_unmapped", "low_vote")


@tool(
    "find_grouping_issues",
    "Worst-first examples of one kind of Group faces problem in a run (latest by "
    "default). kinds: 'split_persons' = a Synology person whose labelled faces are "
    "spread over several groups (sorted by faces outside their main group); "
    "'mixed_clusters' = groups whose labelled faces name different persons (sorted "
    "by minority faces; an unlisted person beside a listed one is often the same "
    "human, a merge rather than a grouping error); 'large_unmapped' = big groups "
    "no person was mapped to, with the reason; 'low_vote' = mapped groups with a "
    "weak majority. Each item has cluster ids for get_cluster and face ids for "
    "explain_face / get_face_crops.",
    properties={
        "kind": {"type": "string", "enum": list(_ISSUE_KINDS)},
        "run_id": {"type": "integer", "minimum": 1},
        "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_LIMIT, "default": _DEFAULT_LIMIT},
        "min_faces": {
            "type": "integer",
            "minimum": 1,
            "description": "Ignore persons / groups with fewer labelled faces (or, for "
            "large_unmapped, a smaller size). Default 3 (large_unmapped: new_person_min_faces).",
        },
        "max_vote_fraction": {
            "type": "number",
            "description": "low_vote: upper bound (default majority + 0.15).",
        },
        "listed_only": {
            "type": "boolean",
            "default": False,
            "description": "split_persons / mixed_clusters: only count persons Synology lists.",
        },
    },
    required=["kind"],
)
def find_grouping_issues(ctx: ToolContext, args: dict) -> dict:
    kind = arg_str(args, "kind", choices=_ISSUE_KINDS)
    if kind is None:
        raise ToolError("'kind' is required")
    limit = _limit(args)
    listed_only = arg_bool(args, "listed_only", False)
    with _db(ctx) as conn:
        lib, fp = _library(conn)
        run = _run(conn, fp, _run_arg(args))
        labels = lib.labels(conn, ctx.settings)[0]
    rl = _run_labels(lib, run, labels)
    default_min = int(run.crossref("new_person_min_faces", 5)) if kind == "large_unmapped" else 3
    min_faces = int(arg_int(args, "min_faces", default_min, lo=1))

    def keep(pk: PersonKey) -> bool:
        return not listed_only or pk in lib.known

    items: list[tuple[Any, dict]] = []
    if kind == "split_persons":
        for pk, per_cluster in rl.by_person.items():
            if not keep(pk):
                continue
            total = sum(per_cluster.values())
            if total < min_faces or len(per_cluster) < 2:
                continue
            main_cid, main_n = max(per_cluster.items(), key=lambda kv: (kv[1], -kv[0]))
            outside = total - main_n
            in_singletons = sum(
                n for cid, n in per_cluster.items() if run.clusters.get(cid) and run.clusters[cid].size == 1
            )
            sample_outside = sorted(
                fid for fid, p in rl.labels.items()
                if p == pk and run.member_of.get(fid) not in (None, main_cid)
            )[:8]
            items.append(
                (
                    (-outside, -total),
                    {
                        "person": lib.person(pk),
                        "labelled_faces_in_run": total,
                        "clusters": len(per_cluster),
                        "faces_outside_main_cluster": outside,
                        "faces_in_one_face_groups": in_singletons,
                        "share_in_main_cluster": _r(main_n / total, 3),
                        "main_cluster": {**_cluster_brief(lib, run, main_cid), "faces_of_person": main_n},
                        "other_clusters": [
                            {**_cluster_brief(lib, run, cid), "faces_of_person": n}
                            for cid, n in sorted(per_cluster.items(), key=lambda kv: (-kv[1], kv[0]))
                            if cid != main_cid
                        ][:5],
                        "sample_face_ids_outside_main": sample_outside,
                        "labelled_faces_not_in_run": rl.not_in_run.get(pk, 0),
                    },
                )
            )
    elif kind == "mixed_clusters":
        for cid, votes in rl.votes.items():
            v = Counter({pk: n for pk, n in votes.items() if keep(pk)})
            total = sum(v.values())
            if len(v) < 2 or total < min_faces:
                continue
            top = max(v.values())
            listed = [pk for pk in v if pk in lib.known]
            items.append(
                (
                    (-(total - top), -total),
                    {
                        **_cluster_brief(lib, run, cid),
                        "labelled_faces": total,
                        "minority_faces": total - top,
                        "distinct_persons": len(v),
                        "distinct_listed_persons": len(listed),
                        "breakdown": _breakdown(lib, v, rl, cid),
                    },
                )
            )
    elif kind == "large_unmapped":
        for cid, c in run.clusters.items():
            if c.mapped is not None or c.size < min_faces:
                continue
            fids = run.members.get(cid, [])
            live = [fid for fid in fids if lib.face_live(fid)]
            photos = {(lib.faces[f].space, lib.faces[f].photo_id) for f in live if f in lib.faces}
            yolo = sum(1 for f in live if f in lib.faces and lib.faces[f].detector == _UNCONFIRMED_DETECTOR)
            votes = rl.votes.get(cid, Counter())
            items.append(
                (
                    (-c.size, cid),
                    {
                        **_cluster_brief(lib, run, cid),
                        "distinct_photos": len(photos),
                        "yolo_only_share": _r(yolo / len(live), 3) if live else None,
                        "labelled_faces": sum(votes.values()),
                        "why_unmapped": _why_unmapped(run, lib, votes, c),
                        "breakdown": _breakdown(lib, votes, rl, cid, limit=4),
                        "sample_face_ids": live[:8],
                    },
                )
            )
    else:
        majority = float(run.crossref("majority", 0.6))
        bound = _arg_float(args, "max_vote_fraction", min(1.0, majority + 0.15))
        for cid, c in run.clusters.items():
            if c.mapped is None or c.vote_fraction is None or c.vote_fraction >= bound:
                continue
            votes = rl.votes.get(cid, Counter())
            if sum(votes.values()) < min_faces:
                continue
            items.append(
                (
                    (c.vote_fraction, -c.size),
                    {
                        **_cluster_brief(lib, run, cid),
                        "labelled_faces": sum(votes.values()),
                        "breakdown": _breakdown(lib, votes, rl, cid),
                    },
                )
            )

    items.sort(key=lambda p: p[0])
    return {
        "kind": kind,
        "run_id": run.run_id,
        "total": len(items),
        "items": [it for _, it in items[:limit]],
        "run_settings": {
            "edge_threshold": run.clustering("edge_threshold"),
            "knn_k": run.clustering("knn_k"),
            "algorithm": run.clustering("algorithm"),
            "majority": run.crossref("majority"),
            "min_labeled": run.crossref("min_labeled"),
            "new_person_min_faces": run.crossref("new_person_min_faces"),
            "merge_vote_fraction": run.crossref("merge_vote_fraction"),
        },
    }


@tool(
    "get_cluster",
    "One group from a Group faces run: size, the person it maps to (and whether "
    "Synology lists them), the label breakdown, detector mix, sample members "
    "(labelled first) with score, quality, size and label, similarity of members "
    "to the group's centroid with the least similar members (likely intruders), "
    "and the nearest other groups by centroid similarity compared with "
    "merge_centroid_sim. Use for a cluster id from grouping_summary, "
    "find_grouping_issues, inspect_photo or explain_face.",
    properties={
        "cluster_id": {"type": "integer", "minimum": 0},
        "run_id": {"type": "integer", "minimum": 1},
        "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_LIMIT, "default": 24},
        "similarity": {
            "type": "boolean",
            "default": True,
            "description": "Compute centroid similarities (loads embeddings; slower the first time).",
        },
    },
    required=["cluster_id"],
)
def get_cluster(ctx: ToolContext, args: dict) -> dict:
    cid = int(arg_int(args, "cluster_id", lo=0))
    limit = _limit(args, 24)
    want_sim = arg_bool(args, "similarity", True)
    tuning = _tuning(ctx)
    with _db(ctx) as conn:
        lib, fp = _library(conn)
        run = _run(conn, fp, _run_arg(args))
        if cid not in run.clusters:
            raise ToolError(f"run {run.run_id} has no cluster {cid}")
        labels = lib.labels(conn, ctx.settings)[0]
        fused = _fused(ctx, conn, fp, _weights_for(run, tuning)) if want_sim else None

    rl = _run_labels(lib, run, labels)
    c = run.clusters[cid]
    fids = run.members.get(cid, [])
    live = [f for f in fids if lib.face_live(f) and f in lib.faces]
    votes = rl.votes.get(cid, Counter())
    detectors = Counter(lib.faces[f].detector for f in live)
    photos = {(lib.faces[f].space, lib.faces[f].photo_id) for f in live}

    ordered = sorted(
        live,
        key=lambda f: (f not in rl.labels, -(lib.faces[f].det_score or 0.0), f),
    )
    out: dict[str, Any] = {
        "run_id": run.run_id,
        **_cluster_brief(lib, run, cid),
        "labeled_count_stored": c.labeled_count,
        "faces_on_deleted_photos": len(fids) - len(live),
        "distinct_photos": len(photos),
        "detectors": dict(detectors.most_common()),
        "labelled_faces": sum(votes.values()),
        "breakdown": _breakdown(lib, votes, rl, cid, limit=10),
        "why_unmapped": None if c.mapped is not None else _why_unmapped(run, lib, votes, c),
        "members": [_face_brief(lib, f, labels) for f in ordered[:limit]],
        "members_total": len(live),
    }

    if fused is not None and live:
        pos = [fused.pos_of[f] for f in live if f in fused.pos_of]
        if pos:
            P = fused.X[pos]
            s = P.sum(axis=0)
            norm = float(np.linalg.norm(s))
            centroid = s / norm if norm else s
            to_centroid = P @ centroid
            n = len(pos)
            mean_pair = (norm * norm - n) / (n * (n - 1)) if n > 1 else None
            order = np.argsort(to_centroid, kind="stable")
            pos_fids = [f for f in live if f in fused.pos_of]
            out["similarity"] = {
                "faces_with_embeddings": n,
                "mean_pairwise": _r(mean_pair, 4),
                "to_centroid": _quantiles(to_centroid.tolist(), 4),
                "least_similar_members": [
                    {
                        **_face_brief(lib, pos_fids[i], labels),
                        "similarity_to_centroid": _r(to_centroid[i], 4),
                    }
                    for i in order[: min(5, n)].tolist()
                ],
            }
            cents = _centroids(fused, run, fp)
            t = cents.index[cid]
            valid = cents.pos_cluster >= 0
            proj = fused.X @ s
            num = np.bincount(
                cents.pos_cluster[valid], weights=proj[valid], minlength=len(cents.cluster_ids)
            )
            denom = cents.norms * norm
            with np.errstate(divide="ignore", invalid="ignore"):
                sims = np.where(denom > 0, num / denom, -1.0)
            sims[t] = -np.inf
            nearest = np.argsort(-sims, kind="stable")[:5]
            out["nearest_clusters"] = [
                {**_cluster_brief(lib, run, cents.cluster_ids[j]), "centroid_similarity": _r(sims[j], 4)}
                for j in nearest.tolist()
                if np.isfinite(sims[j])
            ]
            out["merge_centroid_sim"] = run.crossref("merge_centroid_sim")
        else:
            out["similarity"] = None
    return out


def _review_mentions(
    conn: Connection, voided: frozenset[int], pk: PersonKey, limit: int
) -> dict:
    space, pid = pk
    counts: dict[str, Counter] = defaultdict(Counter)
    sample = []
    for row in conn.execute(
        "SELECT item_id, kind, status, payload_json, confidence FROM review_queue ORDER BY item_id DESC"
    ):
        item_id = int(row["item_id"])
        if item_id in voided:
            continue
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        roles = []
        p_space = str(payload.get("space")) if payload.get("space") is not None else None
        for key, role in (
            ("person_id", "target"),
            ("from_person_id", "from"),
            ("original_person_id", "overruled"),
        ):
            try:
                if payload.get(key) is not None and int(payload[key]) == pid and p_space == space:
                    roles.append(role)
            except (TypeError, ValueError):
                pass
        for side in ("person_a", "person_b"):
            ref = payload.get(side)
            if isinstance(ref, dict):
                try:
                    if str(ref.get("space")) == space and int(ref.get("person_id")) == pid:
                        roles.append("merge")
                except (TypeError, ValueError):
                    pass
        if not roles:
            continue
        counts[str(row["kind"])][str(row["status"])] += 1
        if len(sample) < limit:
            sample.append(
                {
                    "item_id": item_id,
                    "kind": row["kind"],
                    "status": row["status"],
                    "role": roles[0],
                    "confidence": _r(row["confidence"], 3),
                    "face_id": payload.get("face_id"),
                }
            )
    return {"by_kind": {k: dict(v) for k, v in sorted(counts.items())}, "recent": sample}


@tool(
    "get_person",
    "One Synology person, by person_id or by name (case-insensitive; several "
    "matches return candidates to pick from): whether Synology lists them (an "
    "unlisted person is never a target), their Synology boxes and how many we "
    "detected or missed, our faces labelled as them, the groups those faces fall "
    "in (latest run unless run_id), groups mapped to them, and review items that "
    "name them. Use to answer 'why is X split / why are X's faces not suggested'.",
    properties={
        "space": {"type": "string", "enum": list(_SPACES)},
        "person_id": {"type": "integer"},
        "name": {"type": "string"},
        "run_id": {"type": "integer", "minimum": 1},
        "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_LIMIT, "default": 20},
    },
)
def get_person(ctx: ToolContext, args: dict) -> dict:
    space = _space_arg(ctx, args)
    person_id = arg_int(args, "person_id")
    name = arg_str(args, "name")
    limit = _limit(args, 20)
    if person_id is None and not name:
        raise ToolError("give person_id or name")
    with _db(ctx) as conn:
        lib, fp = _library(conn)
        if person_id is not None:
            pk = (space or (list(ctx.settings.nas.spaces) or ["personal"])[0], int(person_id))
        else:
            wanted = name.strip().casefold()
            pool = [
                (k, p) for k, p in lib.persons.items()
                if (space is None or k[0] == space) and p["name"]
            ]
            exact = [k for k, p in pool if p["name"].strip().casefold() == wanted]
            found = exact or [k for k, p in pool if wanted in p["name"].casefold()]
            if not found:
                raise ToolError(f"no person named like {name!r}")
            if len(found) > 1:
                return {
                    "matches": [
                        {**lib.person(k), "item_count": lib.persons[k]["item_count"]}
                        for k in sorted(found)[:limit]
                    ],
                    "note": "several persons match; call again with space and person_id",
                }
            pk = found[0]
        labels, syno_ids = lib.labels(conn, ctx.settings)
        try:
            run = _run(conn, fp, _run_arg(args))
        except ToolError:
            if _run_arg(args) is not None:
                raise
            run = None
        voided = _voided(ctx, conn)
        review = _review_mentions(conn, voided, pk, limit)

    boxes = [(pid, sf) for pid, sf in lib.syno_by_person().get(pk, []) if lib.photo_live((pk[0], pid))]
    ours = sorted(fid for fid, p in labels.items() if p == pk and lib.face_live(fid))
    if pk not in lib.persons and not boxes and not ours:
        raise ToolError(f"no person {pk[0]}/{pk[1]} in Synology's persons, boxes or our labels")

    geo = lib.geometry()
    verdicts = Counter(geo.syno.get((pk[0], sf.syno_face_id), ("unknown",))[0] for _, sf in boxes)
    missed = [
        {"photo_id": pid, "syno_face_id": sf.syno_face_id}
        for pid, sf in boxes
        if geo.syno.get((pk[0], sf.syno_face_id), ("",))[0] == "missed"
    ][:10]
    meta = lib.persons.get(pk)
    out: dict[str, Any] = {
        "person": {
            **lib.person(pk),
            "in_synology_person_list": meta is not None,
            "item_count": meta["item_count"] if meta else None,
            "shown": meta["show"] if meta else None,
            "deleted": meta["deleted"] if meta else None,
        },
        "synology_boxes": {
            "total": len(boxes),
            "by_verdict": dict(verdicts.most_common()),
            "missed_examples": missed,
        },
        "our_faces": {
            "labelled": len(ours),
            "via_box_match": sum(1 for f in ours if f in syno_ids),
            "via_one_face_photo_fallback": sum(1 for f in ours if f not in syno_ids),
            "by_detector": dict(Counter(lib.faces[f].detector for f in ours).most_common()),
            "sample": [_face_brief(lib, f) for f in ours[:limit]],
        },
        "review_items": review,
    }
    if run is not None:
        per_cluster = Counter(run.member_of[f] for f in ours if f in run.member_of)
        mapped_here = [cid for cid, c in run.clusters.items() if c.mapped == pk]
        untagged_in_mapped = sum(
            1
            for cid in mapped_here
            for f in run.members.get(cid, [])
            if f not in labels and lib.face_live(f)
        )
        out["grouping"] = {
            "run_id": run.run_id,
            "labelled_faces_in_run": sum(per_cluster.values()),
            "labelled_faces_not_in_run": len(ours) - sum(per_cluster.values()),
            "clusters_holding_their_faces": len(per_cluster),
            "clusters": [
                {**_cluster_brief(lib, run, cid), "faces_of_person": n}
                for cid, n in sorted(per_cluster.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
            ],
            "clusters_mapped_to_them": [_cluster_brief(lib, run, cid) for cid in mapped_here][:limit],
            "untagged_faces_in_mapped_clusters": untagged_in_mapped,
        }
    if pk not in lib.known:
        out["note"] = (
            "Synology does not list this person. Its faces still label ours and vote "
            "in merges, but it is never a mapping, assign or reassign target "
            "(ADR 19, ADR 20)."
        )
    return out


# --------------------------------------------------------------------------- #
# explain_face                                                                  #
# --------------------------------------------------------------------------- #
def _rank_in(sims_row: np.ndarray, target_pos: int) -> int:
    """0-based rank of ``target_pos`` in a self-excluded similarity row."""
    return int((sims_row > sims_row[target_pos]).sum())


@tool(
    "explain_face",
    "Why one face sits in the group it does: its detection details, Synology "
    "label (listed or not), its group and that group's mapping, and its k most "
    "similar faces by the fused similarity Group faces uses, each with whether "
    "the edge between them clears edge_threshold and survives the kNN graph "
    "(which is what chains faces into a group), plus their groups and labels. "
    "Also scores the face against every listed person the way the "
    "nearest-person step does (mean similarity to that person's 5 closest tagged "
    "faces vs nearest_person_sim and nearest_person_margin). Pass other_face_id "
    "to explain one pair directly: 'why are these two together / apart'.",
    properties={
        "face_id": {"type": "integer"},
        "k": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
        "other_face_id": {"type": "integer"},
        "run_id": {"type": "integer", "minimum": 1},
    },
    required=["face_id"],
)
def explain_face(ctx: ToolContext, args: dict) -> dict:
    fid = int(arg_int(args, "face_id"))
    k = int(arg_int(args, "k", 10, lo=1, hi=50))
    other = arg_int(args, "other_face_id")
    tuning = _tuning(ctx)
    with _db(ctx) as conn:
        lib, fp = _library(conn)
        if fid not in lib.faces:
            raise ToolError(f"face {fid} does not exist (a re-detect renumbers faces)")
        if other is not None and other not in lib.faces:
            raise ToolError(f"face {other} does not exist")
        try:
            run = _run(conn, fp, _run_arg(args))
        except ToolError:
            if _run_arg(args) is not None:
                raise
            run = None
        labels, syno_ids = lib.labels(conn, ctx.settings)
        fused = _fused(ctx, conn, fp, _weights_for(run, tuning))
        own_models = sorted(
            str(r["model"])
            for r in conn.execute(
                "SELECT model FROM embeddings WHERE face_id = ? AND variant = 'orig'", (fid,)
            )
        )

    face = lib.faces[fid]
    geo = lib.geometry()
    knn_k = int(run.clustering("knn_k")) if run else tuning.clustering.knn_k
    edge = float(run.clustering("edge_threshold")) if run else tuning.clustering.edge_threshold
    algorithm = run.clustering("algorithm") if run else tuning.clustering.algorithm
    cfg = tuning.crossref

    def cluster_of(f: int) -> int | None:
        return run.member_of.get(f) if run else None

    out: dict[str, Any] = {
        "face": {
            **_face_brief(lib, fid),
            "det_score_secondary": _r(face.det_score_secondary, 3),
            "pipeline_version": face.pipeline_version,
            "photo_deleted": not lib.face_live(fid),
        },
        "synology": {
            "box_match": geo.faces.get(fid, ("unknown",))[0],
            "label": lib.person(labels.get(fid)),
            "label_source": (
                None if fid not in labels else "box" if fid in syno_ids else "one_face_photo"
            ),
            "syno_face_id": syno_ids.get(fid),
        },
        "cluster": (
            None
            if cluster_of(fid) is None
            else {"run_id": run.run_id, **_cluster_brief(lib, run, cluster_of(fid))}
        ),
        "graph_settings": {
            "run_id": run.run_id if run else None,
            "edge_threshold": edge,
            "knn_k": knn_k,
            "algorithm": algorithm,
            "fusion_weights": fused.weights,
            "edge_threshold_now": tuning.clustering.edge_threshold,
        },
    }
    pos = fused.pos_of.get(fid)
    if pos is None:
        reasons = []
        if not lib.face_live(fid):
            reasons.append("its photo is marked deleted, so grouping leaves it out")
        missing = sorted(set(fused.models) - set(own_models))
        if missing:
            reasons.append(f"it lacks embeddings for {', '.join(missing)}")
        out["neighbours"] = None
        out["not_grouped_because"] = reasons or ["it has no embeddings"]
        return out

    X = fused.X
    sims = X @ X[pos]
    sims[pos] = -np.inf
    graph = _graph(ctx, fused, knn_k)
    top = np.argsort(-sims, kind="stable")[:k]
    my_knn = set(graph[0][pos].tolist()) if graph is not None else None

    def in_knn_of(src: int, dst: int, src_row: np.ndarray | None = None) -> bool:
        if graph is not None:
            return dst in set(graph[0][src].tolist())
        row = src_row if src_row is not None else X @ X[src]
        row = row.copy()
        row[src] = -np.inf
        return _rank_in(row, dst) < knn_k

    my_cluster = cluster_of(fid)
    neighbours = []
    kept = 0
    for rank, j in enumerate(top.tolist()):
        nfid = int(fused.face_ids[j])
        sim = float(sims[j])
        mine = (j in my_knn) if my_knn is not None else rank < knn_k
        theirs = in_knn_of(j, pos)
        edge_kept = sim >= edge and (mine or theirs)
        kept += int(edge_kept)
        ncl = cluster_of(nfid)
        neighbours.append(
            {
                **_face_brief(lib, nfid, labels),
                "similarity": _r(sim, 4),
                "rank": rank + 1,
                "above_edge_threshold": sim >= edge,
                "in_this_faces_knn": mine,
                "this_face_in_their_knn": theirs,
                "edge_kept": edge_kept,
                "cluster_id": ncl,
                "same_cluster": ncl is not None and ncl == my_cluster,
            }
        )
    out["neighbours"] = neighbours
    out["knn_source"] = "cached kNN graph" if graph is not None else "computed"
    out["edges_kept_among_shown"] = kept
    out["best_similarity"] = _r(float(sims[top[0]]), 4) if top.size else None
    if algorithm == "hdbscan":
        out["note"] = "HDBSCAN ignores edge_threshold; edge_kept is only indicative."

    out["nearest_persons"] = _nearest_persons(lib, fused, labels, pos, fid, cfg)

    if other is not None:
        opos = fused.pos_of.get(other)
        if opos is None:
            out["pair"] = {"other_face_id": other, "note": "the other face is not grouped"}
        else:
            sim = float(X[pos] @ X[opos])
            orow = X @ X[opos]
            orow[opos] = -np.inf
            a_rank = _rank_in(sims, opos)
            b_rank = _rank_in(orow, pos)
            a_in = (opos in my_knn) if my_knn is not None else a_rank < knn_k
            b_in = in_knn_of(opos, pos, X @ X[opos])
            ocl = cluster_of(other)
            out["pair"] = {
                "other": {**_face_brief(lib, other, labels), "cluster_id": ocl},
                "similarity": _r(sim, 4),
                "rank_of_other_among_this_faces_neighbours": a_rank + 1,
                "rank_of_this_among_other_faces_neighbours": b_rank + 1,
                "above_edge_threshold": sim >= edge,
                "direct_edge_kept": sim >= edge and (a_in or b_in),
                "same_cluster": ocl is not None and ocl == my_cluster,
                "note": (
                    "Two faces can share a group without a direct edge (a chain of "
                    "edges through others) and sit apart despite one (label "
                    "propagation let a denser neighbourhood win)."
                ),
            }
    return out


def _nearest_persons(
    lib: _Library, fused: _Fused, labels: dict[int, PersonKey], pos: int, fid: int, cfg
) -> dict:
    face = lib.faces[fid]
    by_person: dict[PersonKey, list[int]] = defaultdict(list)
    own = labels.get(fid)
    for lfid, pk in labels.items():
        if pk[0] != face.space or lfid == fid:
            continue
        lpos = fused.pos_of.get(lfid)
        f = lib.faces.get(lfid)
        if lpos is None or f is None or f.detector == _UNCONFIRMED_DETECTOR:
            continue
        if pk in lib.known or pk == own:
            by_person[pk].append(lpos)
    if not by_person:
        return {"candidates": [], "note": "no tagged faces of listed persons in this space"}
    scores = []
    x = fused.X[pos]
    for pk, positions in by_person.items():
        s = fused.X[positions] @ x
        kk = min(_NEAREST_TOPK, s.size)
        topk = np.partition(s, s.size - kk)[-kk:]
        scores.append((float(topk.mean()), pk, len(positions)))
    candidates = sorted((s for s in scores if s[1] in lib.known), key=lambda t: -t[0])
    best = candidates[0] if candidates else None
    runner = candidates[1] if len(candidates) > 1 else None
    margin = best[0] - runner[0] if best and runner else None
    own_score = next((s for s in scores if s[1] == own), None)
    eligible = (
        best is not None
        and face.detector != _UNCONFIRMED_DETECTOR
        and best[0] >= cfg.nearest_person_sim
        and (margin is None or margin >= cfg.nearest_person_margin)
        and best[2] >= cfg.min_labeled
        and best[1] != own
    )
    return {
        "candidates": [
            {**lib.person(pk), "score": _r(score, 4), "tagged_faces": n}
            for score, pk, n in candidates[:5]
        ],
        "margin_over_runner_up": _r(margin, 4),
        "own_label_score": _r(own_score[0], 4) if own_score else None,
        "nearest_person_sim": cfg.nearest_person_sim,
        "nearest_person_margin": cfg.nearest_person_margin,
        "min_labeled": cfg.min_labeled,
        "clears_nearest_person_thresholds": bool(eligible),
        "note": (
            "Scores use current tags of listed persons (an unlisted own label is "
            "scored but is never a candidate). The step only runs for faces outside a "
            "mapped group or whose label disagrees with it, and a YOLO-only face is "
            "never scored, so clearing the thresholds is necessary, not sufficient."
        ),
    }


TOOLS: list[Tool] = [
    detection_summary,
    find_detection_gaps,
    inspect_photo,
    render_photo,
    list_extract_skips,
    grouping_summary,
    find_grouping_issues,
    get_cluster,
    get_person,
    explain_face,
]
