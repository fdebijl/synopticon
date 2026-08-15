"""End-to-end runner tests with injected fake detector/ensemble (no models/network)."""

from __future__ import annotations

import numpy as np
import pytest

from synopticon.config import load_settings
from synopticon.db import store
from synopticon.pipeline.detect.base import Detection
from synopticon.pipeline import runner
from synopticon.pipeline.runner import (
    ExtractStats,
    pipeline_version,
    run_extract,
    skip_reason,
)


# --- fakes ----------------------------------------------------------------

_LM = np.array([[30, 35], [55, 35], [42, 50], [33, 62], [52, 62]], dtype=np.float32)


class FakeDetector:
    """Returns a fixed pair of detections: one landmarked (scrfd), one not (yolo)."""

    def detect(self, img_bgr):
        return [
            Detection(bbox=(20.0, 20.0, 40.0, 40.0), score=0.9, landmarks=_LM.copy(), detector="scrfd"),
            Detection(bbox=(100.0, 100.0, 40.0, 40.0), score=0.7, landmarks=None, detector="yolo"),
        ]


class FakeEnsemble:
    """Deterministic embeddings derived from each crop's mean pixel value."""

    model_versions = {"arcface_r100": "fake", "magface_r100": "fake"}
    available_models = ["arcface_r100", "magface_r100"]

    def run(self, crops):
        arr = np.asarray(crops)
        if arr.ndim == 3:
            arr = arr[None, ...]
        vecs, norms = [], []
        for c in arr:
            s = float(np.mean(c)) / 40.0
            v = np.array([np.cos(s), np.sin(s), 0.2, 0.1], dtype=np.float32)
            v /= np.linalg.norm(v)
            vecs.append(v)
            norms.append(float(50.0 + np.mean(c) / 10.0))
        vecs = np.stack(vecs)
        norms = np.array(norms, dtype=np.float32)
        return {"arcface_r100": vecs.copy(), "magface_r100": vecs.copy()}, norms


def _zeros_restore(ctx_crop_bgr, fidelity):
    return np.zeros_like(ctx_crop_bgr)


# --- fixtures -------------------------------------------------------------

@pytest.fixture
def env(tmp_path):
    conn = store.connect(tmp_path / "synopticon.db")
    settings = load_settings(
        storage={"data_dir": tmp_path, "models_dir": tmp_path / "models"}
    )
    conn.execute(
        "INSERT INTO photos (id, space, type, cache_key, width, height, synced_at, deleted) "
        "VALUES (?,?,?,?,?,?,?,0)",
        (1, "personal", "photo", "ck1", 200, 200, store.now()),
    )
    conn.commit()

    ys, xs = np.mgrid[0:200, 0:200]
    img = np.stack([(xs % 256), (ys % 256), ((xs + ys) % 256)], axis=-1).astype(np.uint8)
    img_path = tmp_path / "orig_1.png"
    import cv2

    cv2.imwrite(str(img_path), img)

    def fetch_original(row):
        return img_path

    return conn, settings, fetch_original


def _run(env, **kw):
    conn, settings, fetch = env
    return run_extract(
        conn, settings, fetch,
        detector_factory=lambda: FakeDetector(),
        ensemble_factory=lambda: FakeEnsemble(),
        **kw,
    )


# --- tests ----------------------------------------------------------------

def test_basic_extract_writes_faces_embeddings_and_log(env):
    conn, settings, _ = env
    stats = _run(env)
    assert isinstance(stats, ExtractStats)
    assert stats.photos_processed == 1
    assert stats.faces_found == 2
    assert stats.detector_counts == {"scrfd": 1, "yolo": 1}

    faces = conn.execute("SELECT * FROM faces ORDER BY face_id").fetchall()
    assert len(faces) == 2
    # scrfd face has landmarks; yolo landmark-less face has NULL landmarks.
    lm_flags = sorted(f["landmarks"] is None for f in faces)
    assert lm_flags == [False, True]
    # quality populated from the MagFace norm.
    assert all(f["quality"] is not None for f in faces)
    # crops written to {crops_dir}/{face_id%256:02x}/.
    for f in faces:
        assert f["crop_path"] and f["ctx_crop_path"]
        from pathlib import Path

        assert Path(f["crop_path"]).is_file()
        assert Path(f["ctx_crop_path"]).is_file()

    embs = conn.execute("SELECT * FROM embeddings").fetchall()
    assert len(embs) == 4  # 2 faces x 2 models
    assert {e["model"] for e in embs} == {"arcface_r100", "magface_r100"}
    assert {e["variant"] for e in embs} == {"orig"}
    assert all(e["model_version"] == "fake" for e in embs)

    log = conn.execute("SELECT * FROM extract_log").fetchall()
    assert len(log) == 1
    assert log[0]["face_count"] == 2
    assert log[0]["cache_key"] == "ck1"


def test_second_run_is_noop(env):
    _run(env)
    stats2 = _run(env)
    assert stats2.photos_processed == 0
    assert stats2.faces_found == 0
    conn = env[0]
    assert conn.execute("SELECT COUNT(*) FROM faces").fetchone()[0] == 2


def test_cache_key_change_triggers_reextract(env):
    conn, settings, _ = env
    _run(env)
    conn.execute("UPDATE photos SET cache_key = 'ck2' WHERE id = 1")
    conn.commit()
    stats = _run(env)
    assert stats.photos_processed == 1
    assert conn.execute("SELECT cache_key FROM extract_log").fetchone()[0] == "ck2"
    # Faces replaced, not duplicated.
    assert conn.execute("SELECT COUNT(*) FROM faces").fetchone()[0] == 2


def test_detection_config_change_bumps_pipeline_version_and_reextracts(env):
    conn, settings, fetch = env
    v1 = pipeline_version(settings, settings.storage.models_dir)
    _run(env)
    # Bump a detection threshold -> new pipeline_version.
    settings.detection.scrfd_score = 0.5
    v2 = pipeline_version(settings, settings.storage.models_dir)
    assert v1 != v2
    stats = _run(env)
    assert stats.photos_processed == 1
    assert conn.execute("SELECT pipeline_version FROM extract_log").fetchone()[0] == v2


def test_photo_id_bypasses_skip_filter(env):
    conn, _, _ = env
    _run(env)
    # Already logged; explicit photo_id reprocesses anyway.
    stats = _run(env, photo_id=1)
    assert stats.photos_processed == 1


def test_restoration_pass_writes_restored_embeddings_and_review_queue(env):
    conn, settings, _ = env
    settings.restoration.enabled = True
    stats = run_extract(
        conn, settings, env[2],
        detector_factory=lambda: FakeDetector(),
        ensemble_factory=lambda: FakeEnsemble(),
        restore_fn=_zeros_restore,
    )
    assert stats.restored == 2  # both faces < trigger_px (40 < 80)
    restored_faces = conn.execute(
        "SELECT * FROM faces WHERE restored = 1"
    ).fetchall()
    assert len(restored_faces) == 2
    assert all(f["restore_disagreement"] is not None for f in restored_faces)

    restored_embs = conn.execute(
        "SELECT COUNT(*) FROM embeddings WHERE variant = 'restored'"
    ).fetchone()[0]
    assert restored_embs == 4  # 2 faces x 2 models

    queue = conn.execute(
        "SELECT * FROM review_queue WHERE kind = 'restore_disagreement'"
    ).fetchall()
    assert len(queue) >= 1
    assert stats.disagreements == len(queue)


def test_bad_photo_is_skipped_with_error(env):
    conn, settings, _ = env

    def bad_fetch(row):
        from pathlib import Path

        return Path("/nonexistent/does_not_exist.png")

    stats = run_extract(
        conn, settings, bad_fetch,
        detector_factory=lambda: FakeDetector(),
        ensemble_factory=lambda: FakeEnsemble(),
    )
    assert stats.skipped == 1
    assert stats.errors == 1
    assert stats.photos_processed == 0
    assert conn.execute("SELECT COUNT(*) FROM faces").fetchone()[0] == 0
    assert stats.skip_reasons == {"the downloaded original is missing from the cache": 1}


def test_skip_is_logged_with_reason_filename_and_link(env, caplog):
    conn, settings, _ = env
    settings.nas.url = "https://nas.example:5001"
    conn.execute("UPDATE photos SET filename = 'IMG_0001.png' WHERE id = 1")
    conn.commit()

    def bad_fetch(row):
        raise OSError("image file is truncated (7 bytes not processed)")

    with caplog.at_level("WARNING", logger="synopticon.pipeline.runner"):
        run_extract(
            conn, settings, bad_fetch,
            detector_factory=lambda: FakeDetector(),
            ensemble_factory=lambda: FakeEnsemble(),
        )

    line = caplog.messages[0]
    assert "skipped photo 1 (IMG_0001.png)" in line
    assert "the image file is truncated or corrupt" in line
    assert "OSError: image file is truncated" in line
    assert "https://nas.example:5001/?launchApp=SYNO.Foto.AppInstance" in line
    assert "timeline/item/1" in line
    # ...and a run-level summary so the per-photo lines needn't be scrolled back to.
    assert any("skipped 1 of 1 photo(s)" in m for m in caplog.messages)


@pytest.mark.parametrize(
    "exc, filename, expected",
    [
        (FileNotFoundError("x"), "a.jpg", "the downloaded original is missing from the cache"),
        (MemoryError(), "a.jpg", "the image is too large to decode safely"),
        (OSError("No space left on device"), "a.jpg", "the disk is full"),
        (ConnectionError("reset"), "a.jpg", "the NAS was unreachable while downloading the original"),
        (ValueError("nope"), "a.jpg", "unexpected error"),
    ],
)
def test_skip_reason_classification(exc, filename, expected):
    assert skip_reason(exc, filename) == expected


def test_skip_reason_names_the_synology_error_code():
    from synopticon.syno.client import SynoApiError

    reason = skip_reason(SynoApiError(105, "SYNO.Foto.Download", "download"), "a.jpg")
    assert reason == "downloading the original from the NAS failed (Synology error code 105)"


def test_skip_reason_points_at_pillow_heif_for_heic(monkeypatch):
    from PIL import UnidentifiedImageError

    monkeypatch.setattr(runner, "_HEIF_AVAILABLE", False)
    assert "pillow-heif" in skip_reason(UnidentifiedImageError("nope"), "IMG.HEIC")

    monkeypatch.setattr(runner, "_HEIF_AVAILABLE", True)
    assert skip_reason(UnidentifiedImageError("nope"), "IMG.HEIC") == "the file is not a decodable image"
