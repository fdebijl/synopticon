"""ArcFace R100 (Glint360K, ``glintr100.onnx``) embedder.

Channel order / scaling follow insightface's ``arcface_onnx.py``
``ArcFaceONNX.get_feat``, which builds the blob with
``cv2.dnn.blobFromImages(imgs, 1.0/127.5, (112,112), (127.5,)*3, swapRB=True)``
— i.e. RGB channel order and ``(x - 127.5) / 127.5`` mapping pixels to
approximately [-1, 1]. Output is a 512-d embedding, L2-normalized here.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from ...config import InferenceConfig
from ..manifest import resolve_model
from ..onnx_session import create_session
from . import base

NAME = "arcface_r100"
DIM = 512
MANIFEST_KEY = "glintr100"

# RGB, (x - 127.5) / 127.5  ==  scale 1/127.5, mean 127.5, swapRB=True.
_SCALE = 1.0 / 127.5
_MEAN = 127.5
_SWAP_RB = True


class ArcFaceEmbedder:
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
    ) -> "ArcFaceEmbedder":
        return cls(resolve_model(models_dir, MANIFEST_KEY), inference)

    def embed(self, crops: np.ndarray) -> np.ndarray:
        blob = base.preprocess(crops, scale=_SCALE, mean=_MEAN, swap_rb=_SWAP_RB)
        vecs = base.run_session(self.session, self.input_name, blob)
        return base.l2_normalize(vecs)
