"""AdaFace IR-101 (WebFace12M) embedder.

AdaFace's reference inference (mk-minchul/AdaFace ``inference.py``) reads images
with OpenCV (**BGR**), then applies ``((tensor/255) - 0.5) / 0.5`` — i.e. it does
NOT swap to RGB and maps [0,255] pixels to [-1,1] as ``(x/255 - 0.5)/0.5``.
Algebraically ``(x/255 - 0.5)/0.5 == (x - 127.5)/127.5``, so we reuse the shared
preprocessing with BGR order (no swapRB). Output is 512-d, L2-normalized here.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from ...config import InferenceConfig
from ..manifest import resolve_model
from ..onnx_session import create_session
from . import base

NAME = "adaface_ir101"
DIM = 512
MANIFEST_KEY = "adaface_ir101_webface12m"

# BGR, (x/255 - 0.5)/0.5 == (x - 127.5)/127.5  ->  scale 1/127.5, mean 127.5, no swap.
_SCALE = 1.0 / 127.5
_MEAN = 127.5
_SWAP_RB = False


class AdaFaceEmbedder:
    name = NAME
    dim = DIM
    MANIFEST_KEY = MANIFEST_KEY

    def __init__(
        self,
        model_path: Path | str,
        inference: InferenceConfig | None = None,
        session=None,
    ):
        if session is None:
            inference = inference or InferenceConfig()
            session = create_session(
                model_path, inference.device, inference.intra_op_threads, inference.device_id
            )
        self.session = session
        self.input_name = session.get_inputs()[0].name

    @classmethod
    def from_manifest(
        cls, models_dir: Path | str, inference: InferenceConfig | None = None
    ) -> "AdaFaceEmbedder":
        return cls(resolve_model(models_dir, MANIFEST_KEY), inference)

    def embed(self, crops: np.ndarray) -> np.ndarray:
        blob = base.preprocess(crops, scale=_SCALE, mean=_MEAN, swap_rb=_SWAP_RB)
        vecs = base.run_session(self.session, self.input_name, blob)
        return base.l2_normalize(vecs)
