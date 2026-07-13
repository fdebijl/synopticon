"""Tests for alignment: norm_crop, resize_crop, context_crop, landmark fallback."""

from __future__ import annotations

import numpy as np

from synopticon.pipeline import align
from synopticon.pipeline.align import ARCFACE_DST, CROP_SIZE
from synopticon.pipeline.detect.base import Detection


def _gradient_image(h=200, w=200):
    ys, xs = np.mgrid[0:h, 0:w]
    img = np.stack([(xs % 256), (ys % 256), ((xs + ys) % 256)], axis=-1)
    return img.astype(np.uint8)


def test_norm_crop_identity_when_landmarks_equal_template():
    img = _gradient_image()
    out = align.norm_crop(img, ARCFACE_DST.copy())
    assert out.shape == (CROP_SIZE, CROP_SIZE, 3)
    assert out.dtype == np.uint8
    # Identity similarity transform -> top-left CROP_SIZE region reproduced.
    assert np.allclose(out.astype(int), img[:CROP_SIZE, :CROP_SIZE].astype(int), atol=1)


def test_norm_crop_accepts_flat_landmarks():
    img = _gradient_image()
    out = align.norm_crop(img, ARCFACE_DST.reshape(-1))
    assert out.shape == (CROP_SIZE, CROP_SIZE, 3)


def test_resize_crop_shape():
    img = _gradient_image()
    out = align.resize_crop(img, (10.0, 10.0, 40.0, 40.0))
    assert out.shape == (CROP_SIZE, CROP_SIZE, 3)


def test_context_crop_clamps_at_border():
    img = _gradient_image(100, 100)
    # bbox in the corner with a large margin -> must clamp, still square output.
    out = align.context_crop(img, (0.0, 0.0, 20.0, 20.0), out_size=256, margin=1.0)
    assert out.shape == (256, 256, 3)


def test_context_crop_center_region():
    img = _gradient_image(300, 300)
    out = align.context_crop(img, (100.0, 100.0, 50.0, 50.0), out_size=128)
    assert out.shape == (128, 128, 3)


class _FakeScrfd:
    def __init__(self, detections):
        self._detections = detections

    def detect(self, img_bgr):
        return list(self._detections)


def test_landmarks_via_scrfd_maps_back_to_full_image():
    img = _gradient_image(200, 200)
    bbox = (50.0, 50.0, 40.0, 40.0)
    # Padded crop origin (pad=0.5): x1=y1=30. Detection is in crop coords.
    lm_crop = np.array([[30, 30], [50, 30], [40, 45], [32, 55], [48, 55]], dtype=np.float32)
    det = Detection(bbox=(20.0, 20.0, 40.0, 40.0), score=0.9, landmarks=lm_crop, detector="scrfd")
    fake = _FakeScrfd([det])
    out = align.landmarks_via_scrfd(fake, img, bbox)
    assert out is not None
    expected = lm_crop + np.array([30, 30], dtype=np.float32)
    assert np.allclose(out, expected)


def test_landmarks_via_scrfd_none_when_no_landmarked_candidates():
    img = _gradient_image(200, 200)
    det = Detection(bbox=(20.0, 20.0, 40.0, 40.0), score=0.9, landmarks=None, detector="yolo")
    fake = _FakeScrfd([det])
    assert align.landmarks_via_scrfd(fake, img, (50.0, 50.0, 40.0, 40.0)) is None


def test_landmarks_via_scrfd_none_when_low_iou():
    img = _gradient_image(200, 200)
    # Detection far from the target region -> IoU below min_iou.
    lm = np.zeros((5, 2), dtype=np.float32)
    det = Detection(bbox=(0.0, 0.0, 3.0, 3.0), score=0.9, landmarks=lm, detector="scrfd")
    fake = _FakeScrfd([det])
    assert align.landmarks_via_scrfd(fake, img, (50.0, 50.0, 40.0, 40.0)) is None
