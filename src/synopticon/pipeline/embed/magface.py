"""MagFace iResNet100 embedder.

MagFace's official inference (IrvingMeng/MagFace ``inference/gen_feat.py``) reads
images with OpenCV (**BGR**) and applies only ``ToTensor()``, i.e. plain
``x / 255`` on BGR channels — no mean subtraction, no swapRB. Verified
empirically against the exported ONNX: x/255 gives same-identity cosine 0.70 /
inter-person max 0.22 on real faces, while (x-127.5)/127.5 collapses the
embedding space (same-identity 0.33 < inter-person 0.46).

MagFace is trained so that the L2 magnitude of the *pre-normalization* embedding
correlates with face quality/recognizability. :meth:`embed_with_norm` exposes
that magnitude alongside the L2-normalized vector; the runner stores it in
``faces.quality`` and uses it to gate restoration.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from ...config import InferenceConfig
from ..manifest import resolve_model
from ..onnx_session import create_session
from . import base

NAME = "magface_r100"
DIM = 512
MANIFEST_KEY = "magface_iresnet100"

# BGR, x/255  ->  scale 1/255, no mean subtraction, no swap.
_SCALE = 1.0 / 255.0
_MEAN = 0.0
_SWAP_RB = False


class MagFaceEmbedder:
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
    ) -> "MagFaceEmbedder":
        return cls(resolve_model(models_dir, MANIFEST_KEY), inference)

    def embed_with_norm(self, crops: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Return (L2-normalized vecs (N, dim), pre-norm L2 magnitudes (N,)).

        The raw magnitude is MagFace's quality signal.
        """
        blob = base.preprocess(crops, scale=_SCALE, mean=_MEAN, swap_rb=_SWAP_RB)
        raw = base.run_session(self.session, self.input_name, blob)
        norms = np.linalg.norm(raw, axis=1).astype(np.float32)
        return base.l2_normalize(raw), norms

    def embed(self, crops: np.ndarray) -> np.ndarray:
        return self.embed_with_norm(crops)[0]
