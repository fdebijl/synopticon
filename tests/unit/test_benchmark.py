"""Benchmark tests with injected fakes: verify timing accounting, warmup, read-only."""

from __future__ import annotations

import numpy as np
import pytest

from synopticon.config import load_settings
from synopticon.db import store
from synopticon.pipeline.benchmark import _STAGES, run_benchmark
from synopticon.pipeline.detect.base import Detection

_LM = np.array([[30, 35], [55, 35], [42, 50], [33, 62], [52, 62]], dtype=np.float32)


class FakeDetector:
    def detect(self, img_bgr):
        return [
            Detection(bbox=(20.0, 20.0, 40.0, 40.0), score=0.9, landmarks=_LM.copy(), detector="scrfd"),
            Detection(bbox=(100.0, 100.0, 40.0, 40.0), score=0.7, landmarks=None, detector="yolo"),
        ]


class FakeEnsemble:
    def run(self, crops):
        arr = np.asarray(crops)
        n = arr.shape[0] if arr.ndim == 4 else 1
        v = np.tile(np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32), (n, 1))
        return {"arcface_r100": v.copy(), "magface_r100": v.copy()}, np.ones(n, dtype=np.float32)


@pytest.fixture
def env(tmp_path):
    conn = store.connect(tmp_path / "synopticon.db")
    settings = load_settings(storage={"data_dir": tmp_path, "models_dir": tmp_path / "models"})

    ys, xs = np.mgrid[0:120, 0:120]
    img = np.stack([(xs % 256), (ys % 256), ((xs + ys) % 256)], axis=-1).astype(np.uint8)
    img_path = tmp_path / "orig.png"
    import cv2

    cv2.imwrite(str(img_path), img)

    for pid in range(1, 11):
        conn.execute(
            "INSERT INTO photos (id, space, type, cache_key, width, height, synced_at, deleted) "
            "VALUES (?,?,?,?,?,?,?,0)",
            (pid, "personal", "photo", f"ck{pid}", 120, 120, store.now()),
        )
    conn.commit()

    def fetch(row):
        return img_path

    return conn, settings, fetch


def _run(env, **kw):
    conn, settings, fetch = env
    return run_benchmark(
        conn, settings, fetch,
        detector_factory=lambda: FakeDetector(),
        ensemble_factory=lambda: FakeEnsemble(),
        **kw,
    )


def test_measures_requested_count_after_warmup(env):
    stats = _run(env, limit=5, warmup=2)
    assert stats.photos == 5  # warmup photos are extra, not subtracted from limit
    assert stats.warmup_photos == 2
    assert stats.faces == 5 * 2  # FakeDetector yields 2 faces per photo


def test_all_stages_accounted_and_compute_is_their_sum(env):
    stats = _run(env, limit=3, warmup=0)
    assert set(stats.stage_s) == set(_STAGES)
    assert stats.compute_s == pytest.approx(sum(stats.stage_s.values()))
    assert all(s >= 0.0 for s in stats.stage_s.values())


def test_writes_nothing(env):
    conn, _, _ = env
    _run(env, limit=4, warmup=1)
    assert conn.execute("SELECT COUNT(*) FROM faces").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM extract_log").fetchone()[0] == 0


def test_photo_id_selects_single_photo(env):
    stats = _run(env, photo_id=3, warmup=0)
    assert stats.photos == 1


def test_empty_selection_reports_gracefully(env):
    stats = _run(env, space="shared", warmup=0)
    assert stats.photos == 0
    assert "no photos measured" in str(stats)


def test_str_renders_rates_and_stage_breakdown(env):
    text = str(_run(env, limit=3, warmup=1))
    assert "3 photos" in text and "ms/photo" in text
    for stage in _STAGES:
        assert stage in text
