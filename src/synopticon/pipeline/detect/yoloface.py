"""YOLOv8-face ONNX inference (derronqi/yolov8-face export).

Letterboxes to 640x640, decodes the (1, C, 8400) output where C is:
  - 5:  cx, cy, w, h, conf                 (no landmarks)
  - 15: ... + 5 landmarks as (x, y) pairs
  - 20: ... + 5 landmarks as (x, y, vis) triples (the common yolov8-face export)

The model is optional (AGPL-licensed, often manually exported); construction
tolerates a missing file — using it then raises a clear error, and the
composite detector simply skips it.
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

MANIFEST_KEY = "yolov8l-face"
_INPUT_SIZE = 640
_PAD_VALUE = 114


def letterbox(img: np.ndarray, size: int = _INPUT_SIZE) -> tuple[np.ndarray, float, float, float]:
    """Resize keeping aspect ratio and center-pad to (size, size).

    Returns (canvas, ratio, pad_x, pad_y).
    """
    h, w = img.shape[:2]
    ratio = min(size / w, size / h)
    new_w, new_h = max(1, round(w * ratio)), max(1, round(h * ratio))
    resized = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_CUBIC)
    canvas = np.full((size, size, 3), _PAD_VALUE, dtype=np.uint8)
    pad_x = (size - new_w) // 2
    pad_y = (size - new_h) // 2
    canvas[pad_y : pad_y + new_h, pad_x : pad_x + new_w] = resized
    return canvas, ratio, float(pad_x), float(pad_y)


def decode_output(
    output: np.ndarray, score_threshold: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray | None]:
    """Decode a YOLOv8-face head output -> (boxes_xyxy, scores, kps|None).

    Accepts (C, N) or (N, C) layouts (channels C <= 32 disambiguates).
    Coordinates are in letterboxed input space.
    """
    preds = np.asarray(output)
    if preds.ndim == 3:
        preds = preds[0]
    if preds.shape[0] <= 32 and preds.shape[0] < preds.shape[1]:
        preds = preds.T  # -> (N, C)
    channels = preds.shape[1]

    scores = preds[:, 4]
    keep = scores >= score_threshold
    preds = preds[keep]
    scores = scores[keep]

    cxcywh = preds[:, :4]
    boxes = np.empty_like(cxcywh)
    boxes[:, 0] = cxcywh[:, 0] - cxcywh[:, 2] / 2
    boxes[:, 1] = cxcywh[:, 1] - cxcywh[:, 3] / 2
    boxes[:, 2] = cxcywh[:, 0] + cxcywh[:, 2] / 2
    boxes[:, 3] = cxcywh[:, 1] + cxcywh[:, 3] / 2

    kps: np.ndarray | None = None
    rest = channels - 5
    if rest == 15:  # 5 x (x, y, visibility)
        kps = preds[:, 5:].reshape(-1, 5, 3)[:, :, :2].copy()
    elif rest == 10:  # 5 x (x, y)
        kps = preds[:, 5:].reshape(-1, 5, 2).copy()
    return boxes.astype(np.float32), scores.astype(np.float32), kps


class YoloFaceDetector:
    """YOLOv8-face detector (secondary, recall-oriented)."""

    name = "yolo"

    def __init__(
        self,
        model_path: Path | str | None,
        detection: DetectionConfig,
        inference: InferenceConfig | None = None,
        session=None,
    ):
        self.detection = detection
        self.session = session
        self.input_name: str | None = None
        self._missing_reason: str | None = None
        if session is None:
            if model_path is None:
                self._missing_reason = "no yolov8l-face model configured"
            else:
                inference = inference or InferenceConfig()
                self.session = create_session(model_path, inference.device, inference.intra_op_threads)
        if self.session is not None:
            self.input_name = self.session.get_inputs()[0].name

    @classmethod
    def from_manifest(
        cls,
        models_dir: Path | str,
        detection: DetectionConfig,
        inference: InferenceConfig | None = None,
    ) -> "YoloFaceDetector":
        try:
            path = resolve_model(models_dir, MANIFEST_KEY)
        except (KeyError, FileNotFoundError) as exc:
            det = cls(None, detection, inference)
            det._missing_reason = str(exc)
            return det
        return cls(path, detection, inference)

    @property
    def available(self) -> bool:
        return self.session is not None

    def _forward(self, img_bgr: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray | None]:
        """One inference pass; returns boxes/scores/kps in img_bgr coordinates."""
        canvas, ratio, pad_x, pad_y = letterbox(img_bgr)
        blob = cv2.dnn.blobFromImage(canvas, 1.0 / 255.0, (_INPUT_SIZE, _INPUT_SIZE), swapRB=True)
        output = self.session.run(None, {self.input_name: blob})[0]
        boxes, scores, kps = decode_output(output, self.detection.yolo_score)
        boxes[:, [0, 2]] = (boxes[:, [0, 2]] - pad_x) / ratio
        boxes[:, [1, 3]] = (boxes[:, [1, 3]] - pad_y) / ratio
        if kps is not None:
            kps[:, :, 0] = (kps[:, :, 0] - pad_x) / ratio
            kps[:, :, 1] = (kps[:, :, 1] - pad_y) / ratio
        return boxes, scores, kps

    def detect(self, img_bgr: np.ndarray) -> list[Detection]:
        if self.session is None:
            raise RuntimeError(f"YOLOv8-face model unavailable: {self._missing_reason}")
        h, w = img_bgr.shape[:2]
        cfg = self.detection

        factors = [1.0]
        if max(h, w) < cfg.yolo_upscale_below_px:
            factors.append(2.0)

        pooled: list[Detection] = []
        for factor in factors:
            if factor != 1.0:
                img_s = cv2.resize(
                    img_bgr,
                    (round(w * factor), round(h * factor)),
                    interpolation=cv2.INTER_CUBIC,
                )
            else:
                img_s = img_bgr
            boxes, scores, kps = self._forward(img_s)
            boxes = boxes / factor
            if kps is not None:
                kps = kps / factor
            for i, (box, score) in enumerate(zip(boxes, scores)):
                x1, y1, x2, y2 = (float(v) for v in box)
                lm = kps[i].astype(np.float32) if kps is not None else None
                pooled.append(
                    Detection(
                        bbox=(x1, y1, x2 - x1, y2 - y1),
                        score=float(score),
                        landmarks=lm,
                        detector=self.name,
                    )
                )
        return nms_detections(pooled, cfg.nms_iou)
