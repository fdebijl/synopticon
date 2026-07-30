"""Extraction runner: detect -> align -> embed -> persist, one photo per txn.

Public entry points (for CLI wiring):

* :func:`run_extract` — process the work queue (photos not yet extracted at the
  current ``pipeline_version``), writing ``faces``, ``embeddings`` and
  ``extract_log`` rows plus aligned/context crops. Crash-resumable: each photo is
  a single SQLite transaction and an ``extract_log`` row marks it done.
* :func:`pipeline_version` — re-exported from the dependency-free
  :mod:`.version` module (a short hash of the model manifest + detection config;
  a change invalidates prior extractions). Import it from there, not from here,
  unless you also want numpy/cv2.

Detector and ensemble are injectable via ``detector_factory`` / ``ensemble_factory``
(zero-arg callables) so tests can run with no model files or network.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Callable

import numpy as np

from ..config import Settings
from ..db import store
from ..progress import get_emitter
from . import align, restore
from .detect.base import Detection
from .detect.merge import union
from .embed.ensemble import MAGFACE_NAME, EmbeddingEnsemble, fuse
from .onnx_session import session_device
from .version import pipeline_version

log = logging.getLogger(__name__)

DetectorFactory = Callable[[], object]
EnsembleFactory = Callable[[], object]
FetchOriginal = Callable[[sqlite3.Row], Path]
RestoreFn = Callable[[np.ndarray, float], np.ndarray]


@dataclass
class ExtractStats:
    photos_processed: int = 0
    faces_found: int = 0
    detector_counts: dict[str, int] = field(default_factory=dict)
    restored: int = 0
    disagreements: int = 0
    skipped: int = 0
    errors: int = 0

    def _bump(self, detector: str) -> None:
        self.detector_counts[detector] = self.detector_counts.get(detector, 0) + 1


class CompositeDetector:
    """SCRFD (primary) + optional YOLOv8-face (secondary) union with landmark
    fallback for YOLO-only boxes."""

    name = "composite"

    def __init__(self, scrfd, yolo, detection):
        self.scrfd = scrfd
        self.yolo = yolo if (yolo is not None and getattr(yolo, "available", False)) else None
        self.detection = detection

    @classmethod
    def from_settings(cls, models_dir: Path | str, settings: Settings) -> "CompositeDetector":
        from .detect.scrfd import ScrfdDetector
        from .detect.yoloface import YoloFaceDetector

        scrfd = ScrfdDetector.from_manifest(models_dir, settings.detection, settings.inference)
        yolo = YoloFaceDetector.from_manifest(models_dir, settings.detection, settings.inference)
        return cls(scrfd, yolo, settings.detection)

    def detect(self, img_bgr: np.ndarray) -> list[Detection]:
        primary = self.scrfd.detect(img_bgr)
        if self.yolo is None:
            return [
                d for d in primary if min(d.bbox[2], d.bbox[3]) >= self.detection.min_face_px
            ]
        secondary = self.yolo.detect(img_bgr)
        dets = union(primary, secondary, self.detection.cross_iou, self.detection.min_face_px)
        for i, d in enumerate(dets):
            if d.landmarks is None:
                lm = align.landmarks_via_scrfd(self.scrfd, img_bgr, d.bbox)
                if lm is not None:
                    dets[i] = replace(d, landmarks=lm)
        return dets


_HEIF_REGISTERED = False


def _ensure_heif() -> None:
    """Register pillow-heif's HEIC/HEIF opener with Pillow (once)."""
    global _HEIF_REGISTERED
    if _HEIF_REGISTERED:
        return
    try:
        from pillow_heif import register_heif_opener
    except ImportError:
        _HEIF_REGISTERED = True  # not installed; treat as done, decode will fail loudly
        return
    register_heif_opener()
    _HEIF_REGISTERED = True


def load_image_bgr(path: Path | str) -> np.ndarray:
    """Decode an image to an EXIF-orientation-corrected BGR uint8 ndarray."""
    from PIL import Image, ImageOps

    _ensure_heif()
    with Image.open(path) as img:
        img = ImageOps.exif_transpose(img)
        rgb = np.asarray(img.convert("RGB"))
    return rgb[:, :, ::-1].copy()  # RGB -> BGR


def _fetch_work(
    conn: sqlite3.Connection,
    space: str,
    version: str,
    limit: int | None,
    photo_id: int | None,
) -> list[sqlite3.Row]:
    if photo_id is not None:
        # Explicit reprocess: bypass the extract_log skip filter.
        return conn.execute(
            "SELECT * FROM photos WHERE space = ? AND id = ? AND deleted = 0",
            (space, photo_id),
        ).fetchall()
    sql = (
        "SELECT p.* FROM photos p "
        "LEFT JOIN extract_log e ON e.space = p.space AND e.photo_id = p.id "
        "WHERE p.deleted = 0 AND p.type = 'photo' AND p.space = ? "
        "AND (e.pipeline_version IS NULL OR e.pipeline_version != ? "
        "     OR e.cache_key IS NOT p.cache_key) "
        "ORDER BY p.id"
    )
    params: list = [space, version]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def _crop_paths(crops_dir: Path, face_id: int) -> tuple[Path, Path]:
    sub = crops_dir / f"{face_id % 256:02x}"
    sub.mkdir(parents=True, exist_ok=True)
    return sub / f"{face_id}.jpg", sub / f"{face_id}_ctx.jpg"


def _aligned_crop(img_bgr: np.ndarray, det: Detection) -> np.ndarray:
    if det.landmarks is not None:
        return align.norm_crop(img_bgr, det.landmarks)
    return align.resize_crop(img_bgr, det.bbox)


def run_extract(
    conn: sqlite3.Connection,
    settings: Settings,
    fetch_original: FetchOriginal,
    limit: int | None = None,
    photo_id: int | None = None,
    space: str = "personal",
    detector_factory: DetectorFactory | None = None,
    ensemble_factory: EnsembleFactory | None = None,
    restore_fn: RestoreFn | None = None,
) -> ExtractStats:
    """Run detection + embedding over the work queue. Returns :class:`ExtractStats`.

    ``fetch_original(row) -> Path`` yields the on-disk original for a photo row.
    ``detector_factory`` / ``ensemble_factory`` (zero-arg) override the default
    model-backed components (injection point for tests). ``restore_fn`` overrides
    :func:`restore.restore_crop`; injecting it also skips the startup extra check.
    """
    import cv2
    from tqdm import tqdm

    models_dir = settings.storage.models_dir
    version = pipeline_version(settings, models_dir)

    if detector_factory is None:
        detector = CompositeDetector.from_settings(models_dir, settings)
    else:
        detector = detector_factory()
    if ensemble_factory is None:
        ensemble = EmbeddingEnsemble(models_dir, settings.inference)
    else:
        ensemble = ensemble_factory()

    restoration_on = settings.restoration.enabled
    if restoration_on and restore_fn is None:
        restore.startup_check(settings)
        restore_fn = restore.restore_crop

    scrfd_session = getattr(getattr(detector, "scrfd", None), "session", None)
    device = session_device(scrfd_session) if scrfd_session is not None else "CPU"
    log.info("extract: running on %s (inference.device=%s)", device, settings.inference.device)

    crops_dir = settings.storage.crops_dir
    stats = ExtractStats()
    rows = _fetch_work(conn, space, version, limit, photo_id)
    emitter = get_emitter()
    log.info(
        "extract: %d photo(s) queued (space=%s, pipeline_version=%s, restoration=%s)",
        len(rows), space, version[:12], "on" if restoration_on else "off",
    )
    # A consumer of the progress protocol renders the structured `progress`
    # events itself; tqdm's carriage-return redraws on stderr would only add
    # noise to the same log.
    for i, row in enumerate(tqdm(rows, desc="extract", unit="photo", disable=emitter.enabled)):
        try:
            _process_photo(
                conn, settings, row, space, version, detector, ensemble,
                fetch_original, crops_dir, restoration_on, restore_fn, stats, cv2,
            )
            conn.commit()
            stats.photos_processed += 1
        except Exception as exc:  # noqa: BLE001 - one bad photo must not abort the run
            conn.rollback()
            stats.skipped += 1
            stats.errors += 1
            log.warning("Skipping photo %s (space=%s): %s", row["id"], space, exc)
            emitter.log("warning", f"skipped photo {row['id']} (space={space}): {exc}", phase="extract")
        emitter.progress("extract", i + 1, len(rows), space=space)

    emitter.result(ok=True, stats=asdict(stats))
    return stats


def _process_photo(
    conn, settings, row, space, version, detector, ensemble,
    fetch_original, crops_dir, restoration_on, restore_fn, stats, cv2,
) -> None:
    pid = int(row["id"])
    cache_key = row["cache_key"] if "cache_key" in row.keys() else None
    path = fetch_original(row)
    img_bgr = load_image_bgr(path)

    dets = detector.detect(img_bgr)

    # Re-extraction: drop prior faces (embeddings cascade) so UNIQUE holds.
    conn.execute("DELETE FROM faces WHERE space = ? AND photo_id = ?", (space, pid))

    face_ids: list[int] = []
    aligned: list[np.ndarray] = []
    ctx_crops: list[np.ndarray] = []
    now = store.now()

    for det in dets:
        x, y, w, h = det.bbox
        lm_blob = (
            store.vec_to_blob(np.asarray(det.landmarks, dtype=np.float32).reshape(-1))
            if det.landmarks is not None
            else None
        )
        cur = conn.execute(
            "INSERT INTO faces (space, photo_id, detector, x, y, w, h, det_score, "
            "det_score_secondary, landmarks, pipeline_version, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                space, pid, det.detector, float(x), float(y), float(w), float(h),
                float(det.score), det.det_score_secondary, lm_blob, version, now,
            ),
        )
        face_id = int(cur.lastrowid)
        face_ids.append(face_id)
        stats._bump(det.detector)

        crop = _aligned_crop(img_bgr, det)
        ctx = align.context_crop(img_bgr, det.bbox)
        crop_path, ctx_path = _crop_paths(crops_dir, face_id)
        cv2.imwrite(str(crop_path), crop)
        cv2.imwrite(str(ctx_path), ctx)
        conn.execute(
            "UPDATE faces SET crop_path = ?, ctx_crop_path = ? WHERE face_id = ?",
            (str(crop_path), str(ctx_path), face_id),
        )
        aligned.append(crop)
        ctx_crops.append(ctx)

    stats.faces_found += len(face_ids)

    fused_orig = None
    if face_ids:
        crops_arr = np.stack(aligned)
        embeddings, mag_norms = ensemble.run(crops_arr)
        for i, face_id in enumerate(face_ids):
            for model, vecs in embeddings.items():
                conn.execute(
                    "INSERT INTO embeddings (face_id, model, variant, dim, vec, "
                    "model_version, created_at) VALUES (?,?,?,?,?,?,?)",
                    (
                        face_id, model, "orig", int(vecs.shape[1]),
                        store.vec_to_blob(vecs[i]),
                        ensemble.model_versions.get(model), now,
                    ),
                )
            if mag_norms is not None:
                conn.execute(
                    "UPDATE faces SET quality = ? WHERE face_id = ?",
                    (float(mag_norms[i]), face_id),
                )
        fused_orig = fuse(embeddings)

        if restoration_on:
            try:
                _restore_pass(
                    conn, settings, space, dets, face_ids, ctx_crops, mag_norms,
                    fused_orig, detector, ensemble, restore_fn, now, stats,
                )
            except Exception as exc:  # noqa: BLE001 - restoration is advisory
                log.warning("Restoration pass failed for photo %s: %s", pid, exc)

    conn.execute(
        "INSERT INTO extract_log (space, photo_id, cache_key, pipeline_version, "
        "face_count, processed_at) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(space, photo_id) DO UPDATE SET "
        "cache_key=excluded.cache_key, pipeline_version=excluded.pipeline_version, "
        "face_count=excluded.face_count, processed_at=excluded.processed_at",
        (space, pid, cache_key, version, len(face_ids), now),
    )


def _restore_pass(
    conn, settings, space, dets, face_ids, ctx_crops, mag_norms, fused_orig,
    detector, ensemble, restore_fn, now, stats,
) -> None:
    threshold = restore.quality_threshold(mag_norms, settings)
    fidelity = settings.restoration.fidelity

    for i, face_id in enumerate(face_ids):
        quality = float(mag_norms[i]) if mag_norms is not None else None
        if not restore.should_restore(dets[i].bbox, quality, settings, threshold):
            continue

        restored_ctx = restore_fn(ctx_crops[i], fidelity)
        cand = [d for d in detector.detect(restored_ctx) if d.landmarks is not None]
        if cand:
            best = max(cand, key=lambda d: d.score)
            aligned_r = align.norm_crop(restored_ctx, best.landmarks)
        else:
            h, w = restored_ctx.shape[:2]
            aligned_r = align.resize_crop(restored_ctx, (0.0, 0.0, float(w), float(h)))

        emb_r, _ = ensemble.run(aligned_r[None])
        for model, vecs in emb_r.items():
            conn.execute(
                "INSERT INTO embeddings (face_id, model, variant, dim, vec, "
                "model_version, created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    face_id, model, "restored", int(vecs.shape[1]),
                    store.vec_to_blob(vecs[0]),
                    ensemble.model_versions.get(model), now,
                ),
            )
        fused_r = fuse(emb_r)
        dis = restore.disagreement(fused_orig[i], fused_r[0])
        conn.execute(
            "UPDATE faces SET restored = 1, restore_disagreement = ? WHERE face_id = ?",
            (dis, face_id),
        )
        stats.restored += 1
        if restore.is_disagreement(dis, settings):
            stats.disagreements += 1
            conn.execute(
                "INSERT INTO review_queue (kind, payload_json, confidence, created_at) "
                "VALUES (?,?,?,?)",
                (
                    "restore_disagreement",
                    json.dumps({"face_id": face_id, "space": space, "disagreement": dis}),
                    dis,
                    now,
                ),
            )
