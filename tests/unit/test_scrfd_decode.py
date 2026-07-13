"""Tests for SCRFD pure decode functions (no ONNX session needed)."""

from __future__ import annotations

import numpy as np

from synopticon.pipeline.detect.scrfd import (
    anchor_centers,
    distance2bbox,
    distance2kps,
)


def test_anchor_centers_layout_and_scaling():
    centers = anchor_centers(2, 2, stride=8, num_anchors=2)
    # 2x2 grid x 2 anchors = 8 rows, each (x, y) scaled by stride.
    assert centers.shape == (8, 2)
    # First location is (0,0); anchors repeated consecutively.
    assert np.array_equal(centers[0], [0, 0])
    assert np.array_equal(centers[1], [0, 0])
    # Row-major over (y, x): next x step is (stride, 0).
    assert np.array_equal(centers[2], [8, 0])
    # Bottom-right location (x=1, y=1) -> (8, 8).
    assert np.array_equal(centers[-1], [8, 8])


def test_anchor_centers_single_anchor():
    centers = anchor_centers(1, 3, stride=16, num_anchors=1)
    assert centers.shape == (3, 2)
    assert np.array_equal(centers, [[0, 0], [16, 0], [32, 0]])


def test_distance2bbox_decodes_ltrb():
    points = np.array([[10.0, 10.0]])
    distance = np.array([[1.0, 2.0, 3.0, 4.0]])  # left, top, right, bottom
    box = distance2bbox(points, distance)
    assert np.allclose(box[0], [9.0, 8.0, 13.0, 14.0])


def test_distance2kps_shape_and_values():
    points = np.array([[5.0, 5.0]])
    distance = np.array([[1, 1, 2, 2, 3, 3, 4, 4, 5, 5]], dtype=np.float32)
    kps = distance2kps(points, distance)
    assert kps.shape == (1, 5, 2)
    assert np.allclose(kps[0, 0], [6, 6])
    assert np.allclose(kps[0, 4], [10, 10])


def test_distance2bbox_batch():
    points = np.array([[0.0, 0.0], [100.0, 100.0]])
    distance = np.array([[1.0, 1.0, 1.0, 1.0], [10.0, 10.0, 10.0, 10.0]])
    boxes = distance2bbox(points, distance)
    assert np.allclose(boxes[0], [-1, -1, 1, 1])
    assert np.allclose(boxes[1], [90, 90, 110, 110])
