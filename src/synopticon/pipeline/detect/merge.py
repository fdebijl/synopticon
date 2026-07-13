"""Detector fusion: IoU helpers, NMS, and the SCRFD+YOLO union. Pure numpy."""

from __future__ import annotations

from dataclasses import replace

import numpy as np

from .base import Detection


def iou_matrix(a_xyxy: np.ndarray, b_xyxy: np.ndarray) -> np.ndarray:
    """Pairwise IoU between two (N,4)/(M,4) xyxy box arrays -> (N,M)."""
    a = np.asarray(a_xyxy, dtype=np.float64).reshape(-1, 4)
    b = np.asarray(b_xyxy, dtype=np.float64).reshape(-1, 4)
    ix1 = np.maximum(a[:, None, 0], b[None, :, 0])
    iy1 = np.maximum(a[:, None, 1], b[None, :, 1])
    ix2 = np.minimum(a[:, None, 2], b[None, :, 2])
    iy2 = np.minimum(a[:, None, 3], b[None, :, 3])
    inter = np.clip(ix2 - ix1, 0, None) * np.clip(iy2 - iy1, 0, None)
    area_a = np.clip(a[:, 2] - a[:, 0], 0, None) * np.clip(a[:, 3] - a[:, 1], 0, None)
    area_b = np.clip(b[:, 2] - b[:, 0], 0, None) * np.clip(b[:, 3] - b[:, 1], 0, None)
    union_area = area_a[:, None] + area_b[None, :] - inter
    with np.errstate(divide="ignore", invalid="ignore"):
        iou = np.where(union_area > 0, inter / union_area, 0.0)
    return iou


def nms(boxes_xyxy: np.ndarray, scores: np.ndarray, iou_threshold: float) -> list[int]:
    """Greedy NMS keeping max-score boxes; returns kept indices (score order)."""
    boxes = np.asarray(boxes_xyxy, dtype=np.float64).reshape(-1, 4)
    scores = np.asarray(scores, dtype=np.float64).ravel()
    if boxes.shape[0] == 0:
        return []
    order = np.argsort(-scores)
    ious = iou_matrix(boxes, boxes)
    kept: list[int] = []
    suppressed = np.zeros(boxes.shape[0], dtype=bool)
    for idx in order:
        if suppressed[idx]:
            continue
        kept.append(int(idx))
        suppressed |= ious[idx] > iou_threshold
        suppressed[idx] = True
    return kept


def nms_detections(dets: list[Detection], iou_threshold: float) -> list[Detection]:
    if not dets:
        return []
    boxes = np.stack([d.xyxy for d in dets])
    scores = np.array([d.score for d in dets])
    return [dets[i] for i in nms(boxes, scores, iou_threshold)]


def union(
    primary: list[Detection],
    secondary: list[Detection],
    cross_iou: float,
    min_face_px: float = 0,
) -> list[Detection]:
    """Fuse primary (SCRFD) and secondary (YOLO) detections.

    Greedy matching by descending secondary score: each secondary detection is
    matched to the not-yet-matched primary with the highest IoU >= cross_iou.
    Matched pairs keep the primary box + landmarks (detector='merged') and
    record the secondary score. Unmatched secondary detections are kept as-is.
    Finally, any detection with min(w, h) < min_face_px is dropped.

    Inputs are not mutated.
    """
    out: list[Detection] = [replace(d) for d in primary]

    if primary and secondary:
        p_boxes = np.stack([d.xyxy for d in primary])
        s_boxes = np.stack([d.xyxy for d in secondary])
        ious = iou_matrix(s_boxes, p_boxes)  # (S, P)
        matched_p: set[int] = set()
        order = np.argsort([-d.score for d in secondary])
        for si in order:
            row = ious[si].copy()
            for pi in matched_p:
                row[pi] = -1.0
            best = int(np.argmax(row))
            if row[best] >= cross_iou:
                matched_p.add(best)
                out[best] = replace(
                    out[best],
                    detector="merged",
                    det_score_secondary=float(secondary[si].score),
                )
            else:
                out.append(replace(secondary[si]))
    elif secondary:
        out.extend(replace(d) for d in secondary)

    return [d for d in out if min(d.bbox[2], d.bbox[3]) >= min_face_px]
