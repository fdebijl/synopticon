"""Detection dataclass + Detector protocol shared by all detectors."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import numpy as np


@dataclass
class Detection:
    """One detected face in EXIF-orientation-corrected image pixel space."""

    bbox: tuple[float, float, float, float]  # x, y, w, h (pixels)
    score: float
    landmarks: np.ndarray | None = field(default=None)  # 5x2 float32, pixel coords
    detector: str = ""  # 'scrfd' | 'yolo' | 'merged'
    det_score_secondary: float | None = None

    @property
    def xyxy(self) -> np.ndarray:
        x, y, w, h = self.bbox
        return np.array([x, y, x + w, y + h], dtype=np.float32)

    @property
    def wh(self) -> tuple[float, float]:
        return self.bbox[2], self.bbox[3]

    @property
    def area(self) -> float:
        return self.bbox[2] * self.bbox[3]


@runtime_checkable
class Detector(Protocol):
    def detect(self, img_bgr: np.ndarray) -> list[Detection]: ...
