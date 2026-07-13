"""Embedder protocol + shared ONNX preprocessing/inference helpers.

An embedder takes a batch of aligned 112x112 BGR uint8 crops (as produced by
``align.norm_crop``) and returns L2-normalized float32 embeddings. Each concrete
embedder differs only in channel order (RGB vs BGR) and the affine scaling
applied before the network; that variation is captured by the ``swap_rb`` /
``scale`` / ``mean`` arguments to :func:`preprocess`.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import numpy as np

INPUT_SIZE = 112


@runtime_checkable
class Embedder(Protocol):
    name: str
    dim: int

    def embed(self, crops: np.ndarray) -> np.ndarray:
        """Embed (N, H, W, 3) BGR uint8 crops -> (N, dim) float32 L2-normalized."""
        ...


def preprocess(
    crops: np.ndarray,
    *,
    scale: float,
    mean: float,
    swap_rb: bool,
) -> np.ndarray:
    """Build an NCHW float32 blob from (N, H, W, 3) BGR uint8 crops.

    ``out = (channels - mean) * scale`` where channels are RGB when
    ``swap_rb`` else BGR. Crops that are not 112x112 are resized.
    """
    import cv2

    arr = np.asarray(crops)
    if arr.ndim == 3:  # single crop -> batch of 1
        arr = arr[None, ...]
    if arr.shape[1:3] != (INPUT_SIZE, INPUT_SIZE):
        arr = np.stack(
            [cv2.resize(c, (INPUT_SIZE, INPUT_SIZE), interpolation=cv2.INTER_CUBIC) for c in arr]
        )
    blob = arr.astype(np.float32)
    if swap_rb:
        blob = blob[..., ::-1]  # BGR -> RGB
    blob = (blob - mean) * scale
    # NHWC -> NCHW, contiguous for onnxruntime.
    return np.ascontiguousarray(blob.transpose(0, 3, 1, 2))


def l2_normalize(vecs: np.ndarray, eps: float = 1e-12) -> np.ndarray:
    """Row-wise L2 normalization -> unit vectors (float32)."""
    vecs = np.asarray(vecs, dtype=np.float32)
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    return (vecs / np.maximum(norms, eps)).astype(np.float32)


def run_session(session, input_name: str, blob: np.ndarray) -> np.ndarray:
    """Run the ONNX session and return the first output as (N, dim) float32."""
    out = session.run(None, {input_name: blob})[0]
    return np.asarray(out, dtype=np.float32).reshape(blob.shape[0], -1)
