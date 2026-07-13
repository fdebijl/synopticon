"""Self-contained SCRFD ONNX inference (no insightface runtime dependency).

Implements the SCRFD decode for the 10G-KPS model: FPN strides 8/16/32,
2 anchors per location, distance-encoded boxes (distance2bbox) and 5-point
landmarks (distance2kps). Preprocessing follows insightface's SCRFD class:
blob = (img - 127.5) / 128 with RGB channel order (swapRB).

Multi-scale: the image is run at each settings.detection.scales factor
(long side capped at max_long_side, INTER_CUBIC upscale), detections are
mapped back to original coordinates, pooled across scales, and NMS'd at
settings.detection.nms_iou keeping the max score.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from ...config import DetectionConfig, InferenceConfig
from ..manifest import resolve_model
from ..onnx_session import create_session
from .base import Detection
from .merge import nms_detections

_STRIDES = (8, 16, 32)
_NUM_ANCHORS = 2
_FMC = 3  # feature map count == len(_STRIDES)

MANIFEST_KEY = "scrfd_10g_bnkps"


def anchor_centers(height: int, width: int, stride: int, num_anchors: int = _NUM_ANCHORS) -> np.ndarray:
    """Anchor center coordinates for one FPN level -> (h*w*num_anchors, 2).

    Row-major over (y, x); each center repeated num_anchors times
    consecutively, matching insightface's decode layout.
    """
    xs, ys = np.meshgrid(np.arange(width), np.arange(height))
    centers = np.stack([xs, ys], axis=-1).astype(np.float32) * stride
    centers = centers.reshape(-1, 2)
    if num_anchors > 1:
        centers = np.repeat(centers, num_anchors, axis=0)
    return centers


def distance2bbox(points: np.ndarray, distance: np.ndarray) -> np.ndarray:
    """Decode (left, top, right, bottom) distances from anchor points -> xyxy."""
    x1 = points[:, 0] - distance[:, 0]
    y1 = points[:, 1] - distance[:, 1]
    x2 = points[:, 0] + distance[:, 2]
    y2 = points[:, 1] + distance[:, 3]
    return np.stack([x1, y1, x2, y2], axis=-1)


def distance2kps(points: np.ndarray, distance: np.ndarray) -> np.ndarray:
    """Decode 5-point landmark offsets from anchor points -> (N, 5, 2)."""
    preds = []
    for i in range(0, distance.shape[1], 2):
        px = points[:, 0] + distance[:, i]
        py = points[:, 1] + distance[:, i + 1]
        preds.append(px)
        preds.append(py)
    return np.stack(preds, axis=-1).reshape(-1, distance.shape[1] // 2, 2)


def _squeeze_batch(arr: np.ndarray) -> np.ndarray:
    arr = np.asarray(arr)
    return arr[0] if arr.ndim == 3 else arr


class ScrfdDetector:
    """SCRFD-10G-KPS detector."""

    name = "scrfd"

    def __init__(
        self,
        model_path: Path | str,
        detection: DetectionConfig,
        inference: InferenceConfig | None = None,
        session=None,
    ):
        self.detection = detection
        if session is None:
            inference = inference or InferenceConfig()
            session = create_session(model_path, inference.device, inference.intra_op_threads)
        self.session = session
        self.input_name = session.get_inputs()[0].name
        self._center_cache: dict[tuple[int, int, int], np.ndarray] = {}

    @classmethod
    def from_manifest(
        cls,
        models_dir: Path | str,
        detection: DetectionConfig,
        inference: InferenceConfig | None = None,
    ) -> "ScrfdDetector":
        return cls(resolve_model(models_dir, MANIFEST_KEY), detection, inference)

    def _centers(self, height: int, width: int, stride: int) -> np.ndarray:
        key = (height, width, stride)
        if key not in self._center_cache:
            self._center_cache[key] = anchor_centers(height, width, stride)
            if len(self._center_cache) > 100:
                self._center_cache.clear()
        return self._center_cache[key]

    def _forward(self, img_bgr: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Run one scale. Returns (boxes_xyxy, scores, kps) in img coordinates."""
        h0, w0 = img_bgr.shape[:2]
        input_h = int(np.ceil(h0 / 32) * 32)
        input_w = int(np.ceil(w0 / 32) * 32)
        padded = np.zeros((input_h, input_w, 3), dtype=np.uint8)
        padded[:h0, :w0] = img_bgr
        blob = cv2.dnn.blobFromImage(
            padded, 1.0 / 128.0, (input_w, input_h), (127.5, 127.5, 127.5), swapRB=True
        )
        outputs = self.session.run(None, {self.input_name: blob})
        if len(outputs) != _FMC * 3:
            raise RuntimeError(
                f"SCRFD model produced {len(outputs)} outputs, expected {_FMC * 3} "
                f"(scores/bbox/kps x strides {list(_STRIDES)}); is this the bnkps variant?"
            )

        boxes_all: list[np.ndarray] = []
        scores_all: list[np.ndarray] = []
        kps_all: list[np.ndarray] = []
        threshold = self.detection.scrfd_score
        for idx, stride in enumerate(_STRIDES):
            scores = _squeeze_batch(outputs[idx]).ravel()
            bbox_preds = _squeeze_batch(outputs[idx + _FMC]) * stride
            kps_preds = _squeeze_batch(outputs[idx + 2 * _FMC]) * stride
            centers = self._centers(input_h // stride, input_w // stride, stride)
            keep = np.where(scores >= threshold)[0]
            if keep.size == 0:
                continue
            boxes_all.append(distance2bbox(centers[keep], bbox_preds[keep]))
            scores_all.append(scores[keep])
            kps_all.append(distance2kps(centers[keep], kps_preds[keep]))

        if not boxes_all:
            empty = np.zeros((0,), dtype=np.float32)
            return np.zeros((0, 4), np.float32), empty, np.zeros((0, 5, 2), np.float32)
        return (
            np.concatenate(boxes_all).astype(np.float32),
            np.concatenate(scores_all).astype(np.float32),
            np.concatenate(kps_all).astype(np.float32),
        )

    def detect(self, img_bgr: np.ndarray) -> list[Detection]:
        h, w = img_bgr.shape[:2]
        long_side = max(h, w)
        cfg = self.detection

        pooled: list[Detection] = []
        seen_factors: set[float] = set()
        for scale in cfg.scales:
            factor = float(scale)
            if long_side * factor > cfg.max_long_side:
                factor = cfg.max_long_side / long_side
            factor = round(factor, 4)
            if factor <= 0 or factor in seen_factors:
                continue
            seen_factors.add(factor)

            if factor != 1.0:
                new_w = max(1, round(w * factor))
                new_h = max(1, round(h * factor))
                img_s = cv2.resize(img_bgr, (new_w, new_h), interpolation=cv2.INTER_CUBIC)
            else:
                img_s = img_bgr
            fx = img_s.shape[1] / w
            fy = img_s.shape[0] / h

            boxes, scores, kps = self._forward(img_s)
            boxes = boxes / np.array([fx, fy, fx, fy], dtype=np.float32)
            kps = kps / np.array([fx, fy], dtype=np.float32)
            for box, score, lm in zip(boxes, scores, kps):
                x1, y1, x2, y2 = (float(v) for v in box)
                pooled.append(
                    Detection(
                        bbox=(x1, y1, x2 - x1, y2 - y1),
                        score=float(score),
                        landmarks=lm.astype(np.float32),
                        detector=self.name,
                    )
                )

        return nms_detections(pooled, cfg.nms_iou)
