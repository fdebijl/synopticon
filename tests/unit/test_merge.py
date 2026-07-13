"""Tests for detector fusion: iou_matrix, nms, union."""

from __future__ import annotations

import numpy as np

from synopticon.pipeline.detect.base import Detection
from synopticon.pipeline.detect.merge import iou_matrix, nms, nms_detections, union


def _det(x, y, w, h, score=0.9, detector="scrfd", landmarks=None, secondary=None):
    return Detection(
        bbox=(x, y, w, h),
        score=score,
        landmarks=landmarks,
        detector=detector,
        det_score_secondary=secondary,
    )


def test_iou_identical_and_disjoint():
    a = np.array([[0, 0, 10, 10]], dtype=np.float32)
    b = np.array([[0, 0, 10, 10], [100, 100, 110, 110]], dtype=np.float32)
    m = iou_matrix(a, b)
    assert m.shape == (1, 2)
    assert m[0, 0] == 1.0
    assert m[0, 1] == 0.0


def test_iou_containment_and_half_overlap():
    a = np.array([[0, 0, 10, 10]], dtype=np.float32)  # area 100
    contained = np.array([[0, 0, 5, 10]], dtype=np.float32)  # area 50, inter 50
    assert iou_matrix(a, contained)[0, 0] == 0.5


def test_nms_suppresses_overlap_keeps_higher_score():
    boxes = np.array([[0, 0, 10, 10], [1, 1, 11, 11], [100, 100, 110, 110]], dtype=np.float32)
    scores = np.array([0.9, 0.8, 0.7])
    kept = nms(boxes, scores, 0.45)
    assert kept[0] == 0  # highest score first, suppresses box 1
    assert 1 not in kept
    assert 2 in kept


def test_nms_empty():
    assert nms(np.zeros((0, 4)), np.zeros((0,)), 0.5) == []


def test_nms_detections_orders_by_score():
    dets = [_det(0, 0, 10, 10, score=0.5), _det(50, 50, 10, 10, score=0.99)]
    out = nms_detections(dets, 0.45)
    assert [d.score for d in out] == [0.99, 0.5]


def test_union_match_records_secondary_and_labels_merged():
    lm = np.zeros((5, 2), dtype=np.float32)
    primary = [_det(0, 0, 10, 10, score=0.8, detector="scrfd", landmarks=lm)]
    secondary = [_det(1, 1, 10, 10, score=0.7, detector="yolo")]
    out = union(primary, secondary, cross_iou=0.5)
    assert len(out) == 1
    assert out[0].detector == "merged"
    assert out[0].det_score_secondary == 0.7
    assert out[0].score == 0.8  # primary box/score retained
    assert out[0].landmarks is not None  # primary landmarks kept


def test_union_unmatched_secondary_kept_as_yolo():
    primary = [_det(0, 0, 10, 10, detector="scrfd")]
    secondary = [_det(500, 500, 20, 20, score=0.6, detector="yolo")]
    out = union(primary, secondary, cross_iou=0.5)
    labels = sorted(d.detector for d in out)
    assert labels == ["scrfd", "yolo"]


def test_union_greedy_multi_match_one_primary_per_secondary():
    # Two secondaries overlap the same primary; only the higher-score one matches.
    primary = [_det(0, 0, 10, 10, score=0.9, detector="scrfd")]
    secondary = [
        _det(0, 0, 10, 10, score=0.8, detector="yolo"),
        _det(1, 1, 10, 10, score=0.6, detector="yolo"),
    ]
    out = union(primary, secondary, cross_iou=0.5)
    merged = [d for d in out if d.detector == "merged"]
    leftover_yolo = [d for d in out if d.detector == "yolo"]
    assert len(merged) == 1
    assert merged[0].det_score_secondary == 0.8  # higher-score secondary won the match
    assert len(leftover_yolo) == 1  # the other secondary stays as its own detection


def test_union_min_face_px_filter_applies_to_both_sources():
    primary = [_det(0, 0, 5, 5, detector="scrfd")]  # too small
    secondary = [_det(200, 200, 4, 30, detector="yolo")]  # min side 4, too small
    out = union(primary, secondary, cross_iou=0.5, min_face_px=20)
    assert out == []


def test_union_does_not_mutate_inputs():
    primary = [_det(0, 0, 10, 10, score=0.8, detector="scrfd")]
    secondary = [_det(0, 0, 10, 10, score=0.7, detector="yolo")]
    union(primary, secondary, cross_iou=0.5)
    assert primary[0].detector == "scrfd"
    assert primary[0].det_score_secondary is None
