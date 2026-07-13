"""Face alignment: ArcFace 112x112 norm_crop, context crops, landmark fallback."""

from __future__ import annotations

import cv2
import numpy as np

from .detect.base import Detection

# Canonical ArcFace 5-point template for 112x112 crops
# (left eye, right eye, nose, left mouth, right mouth) — the standard
# insightface norm_crop destination coordinates.
ARCFACE_DST = np.array(
    [
        [38.2946, 51.6963],
        [73.5318, 51.5014],
        [56.0252, 71.7366],
        [41.5493, 92.3655],
        [70.7299, 92.2041],
    ],
    dtype=np.float32,
)

CROP_SIZE = 112


def norm_crop(img_bgr: np.ndarray, landmarks_5x2: np.ndarray) -> np.ndarray:
    """Similarity-transform crop to the canonical ArcFace 112x112 template."""
    src = np.asarray(landmarks_5x2, dtype=np.float32).reshape(5, 2)
    matrix, _ = cv2.estimateAffinePartial2D(src, ARCFACE_DST, method=cv2.LMEDS)
    if matrix is None:
        # Degenerate landmark sets (collinear/duplicated): least-squares fallback.
        from skimage.transform import SimilarityTransform

        tform = SimilarityTransform()
        tform.estimate(src, ARCFACE_DST)
        matrix = tform.params[:2]
    return cv2.warpAffine(img_bgr, matrix, (CROP_SIZE, CROP_SIZE), borderValue=0)


def resize_crop(img_bgr: np.ndarray, bbox: tuple[float, float, float, float]) -> np.ndarray:
    """Plain resized bbox crop for faces without landmarks (marked in DB by
    landmarks IS NULL)."""
    h, w = img_bgr.shape[:2]
    x, y, bw, bh = bbox
    x1 = int(np.clip(np.floor(x), 0, w - 1))
    y1 = int(np.clip(np.floor(y), 0, h - 1))
    x2 = int(np.clip(np.ceil(x + bw), x1 + 1, w))
    y2 = int(np.clip(np.ceil(y + bh), y1 + 1, h))
    crop = img_bgr[y1:y2, x1:x2]
    return cv2.resize(crop, (CROP_SIZE, CROP_SIZE), interpolation=cv2.INTER_CUBIC)


def context_crop(
    img_bgr: np.ndarray,
    bbox: tuple[float, float, float, float],
    out_size: int = 256,
    margin: float = 0.5,
) -> np.ndarray:
    """Square context crop around the bbox for review UI / restoration.

    The crop side is max(w, h) * (1 + 2*margin) centered on the bbox center,
    clamped to image bounds, then resized to (out_size, out_size).
    """
    h, w = img_bgr.shape[:2]
    x, y, bw, bh = bbox
    side = max(bw, bh) * (1.0 + 2.0 * margin)
    cx, cy = x + bw / 2.0, y + bh / 2.0
    x1 = int(np.clip(round(cx - side / 2), 0, w - 1))
    y1 = int(np.clip(round(cy - side / 2), 0, h - 1))
    x2 = int(np.clip(round(cx + side / 2), x1 + 1, w))
    y2 = int(np.clip(round(cy + side / 2), y1 + 1, h))
    crop = img_bgr[y1:y2, x1:x2]
    return cv2.resize(crop, (out_size, out_size), interpolation=cv2.INTER_CUBIC)


def landmarks_via_scrfd(
    scrfd_detector,
    img_bgr: np.ndarray,
    bbox: tuple[float, float, float, float],
    pad: float = 0.5,
    min_iou: float = 0.10,
) -> np.ndarray | None:
    """Recover landmarks for a landmark-less (YOLO-only) detection.

    Re-runs SCRFD on the bbox crop padded by `pad` on each side, picks the
    detection with the highest IoU against the original bbox, and maps its
    landmarks back to full-image coordinates. Returns None when nothing
    matches (the face is then embedded from a plain resized bbox crop).
    """
    from .detect.merge import iou_matrix

    h, w = img_bgr.shape[:2]
    x, y, bw, bh = bbox
    x1 = int(np.clip(np.floor(x - bw * pad), 0, w - 1))
    y1 = int(np.clip(np.floor(y - bh * pad), 0, h - 1))
    x2 = int(np.clip(np.ceil(x + bw * (1 + pad)), x1 + 1, w))
    y2 = int(np.clip(np.ceil(y + bh * (1 + pad)), y1 + 1, h))
    crop = img_bgr[y1:y2, x1:x2]
    if crop.size == 0:
        return None

    detections: list[Detection] = scrfd_detector.detect(crop)
    candidates = [d for d in detections if d.landmarks is not None]
    if not candidates:
        return None

    target = np.array([[x - x1, y - y1, x - x1 + bw, y - y1 + bh]], dtype=np.float32)
    boxes = np.stack([d.xyxy for d in candidates])
    ious = iou_matrix(boxes, target)[:, 0]
    best = int(np.argmax(ious))
    if ious[best] < min_iou:
        return None
    landmarks = candidates[best].landmarks.astype(np.float32).copy()
    landmarks[:, 0] += x1
    landmarks[:, 1] += y1
    return landmarks
