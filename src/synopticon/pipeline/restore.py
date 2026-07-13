"""Optional face restoration (CodeFormer) + disagreement gating.

Restoration is advisory only: clustering always uses the original ('orig')
embedding. This module contributes three real, unit-tested pieces of logic —
``should_restore`` gating, ``disagreement`` scoring, and ``startup_check`` — plus
a thin ``restore_crop`` wrapper whose heavy body (basicsr/CodeFormer via the
``[restore]`` extra) is intentionally deferred: it raises a clear
NotImplementedError with vendoring instructions until the extra is wired up.

torch/basicsr are imported lazily so this module imports cleanly without them.
"""

from __future__ import annotations

import numpy as np

from ..config import Settings


def should_restore(
    bbox: tuple[float, float, float, float],
    quality_norm: float | None,
    settings: Settings,
    quality_threshold: float | None,
) -> bool:
    """Whether a face should be run through restoration.

    True when the face is small (min bbox side < restoration.trigger_px) OR its
    MagFace quality magnitude falls below the per-batch percentile cut
    (``quality_threshold``, computed by the runner over the batch's norms).
    """
    _, _, w, h = bbox
    if min(w, h) < settings.restoration.trigger_px:
        return True
    if quality_norm is not None and quality_threshold is not None:
        return quality_norm < quality_threshold
    return False


def quality_threshold(norms: np.ndarray | None, settings: Settings) -> float | None:
    """Per-batch quality cut: the configured percentile of the MagFace norms.

    Returns None when there are no norms (MagFace unavailable / empty batch).
    """
    if norms is None:
        return None
    arr = np.asarray(norms, dtype=np.float32).ravel()
    if arr.size == 0:
        return None
    return float(np.percentile(arr, settings.restoration.quality_percentile))


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity between two vectors (robust to non-unit inputs)."""
    a = np.asarray(a, dtype=np.float64).ravel()
    b = np.asarray(b, dtype=np.float64).ravel()
    denom = np.linalg.norm(a) * np.linalg.norm(b)
    if denom == 0:
        return 0.0
    return float(np.dot(a, b) / denom)


def disagreement(fused_orig: np.ndarray, fused_restored: np.ndarray) -> float:
    """Restoration disagreement = ``1 - cos(orig, restored)`` in [0, 2]."""
    return 1.0 - cosine(fused_orig, fused_restored)


def is_disagreement(value: float, settings: Settings) -> bool:
    """Whether a disagreement score should be flagged for review."""
    return value > settings.restoration.disagreement_cos


def startup_check(settings: Settings) -> None:
    """Fail fast at startup if restoration is enabled but the extra is missing.

    Raised here (rather than mid-run) so a multi-hour extract does not die
    thousands of photos in.
    """
    if not settings.restoration.enabled:
        return
    try:
        import torch  # noqa: F401
        import torchvision  # noqa: F401
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise RuntimeError(
            "restoration.enabled=true but the '[restore]' extra is not installed. "
            "Install it (torch==2.1.2, torchvision==0.16.2, basicsr, facexlib) or set "
            "restoration.enabled=false."
        ) from exc


def restore_crop(ctx_crop_bgr: np.ndarray, fidelity: float) -> np.ndarray:
    """Restore a face in a context crop with CodeFormer (fidelity == CodeFormer w).

    NOTE: the CodeFormer/basicsr inference body is intentionally not vendored
    here. To enable restoration:

      1. Install the ``[restore]`` extra (pinned torch 2.1.2 / torchvision 0.16.2;
         basicsr imports torchvision.transforms.functional_tensor, removed in
         torchvision >= 0.17, hence the pins).
      2. Vendor the CodeFormer arch + weights (sczhou/CodeFormer) into models_dir
         and load them here with facexlib's face-restore helper.
      3. Return the restored crop as a BGR uint8 ndarray of the same shape.

    The gating (``should_restore``), disagreement scoring, and review-queue wiring
    around this function are complete and tested; only the model call is deferred.
    """
    raise NotImplementedError(
        "restore_crop is not yet wired up; install the '[restore]' extra and vendor "
        "CodeFormer per the docstring, or keep restoration.enabled=false."
    )
