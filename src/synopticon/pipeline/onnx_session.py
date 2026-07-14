"""Shared onnxruntime session construction (CPU-first, optional CUDA)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal


def physical_cores() -> int:
    """Best-effort physical core count (Linux /proc/cpuinfo, else cpu_count)."""
    import os

    try:
        cores: set[tuple[str, str]] = set()
        physical_id = core_id = ""
        with open("/proc/cpuinfo") as fh:
            for line in fh:
                if line.startswith("physical id"):
                    physical_id = line.split(":", 1)[1].strip()
                elif line.startswith("core id"):
                    core_id = line.split(":", 1)[1].strip()
                elif not line.strip() and core_id != "":
                    cores.add((physical_id, core_id))
                    physical_id = core_id = ""
        if core_id != "":
            cores.add((physical_id, core_id))
        if cores:
            return len(cores)
    except OSError:
        pass
    return os.cpu_count() or 1


_dlls_preloaded = False


def _preload_cuda_dlls(ort, logger) -> None:
    """Make onnxruntime-gpu's pip-installed CUDA/cuDNN wheels loadable.

    The `onnxruntime-gpu[cuda,cudnn]` wheels drop the NVIDIA libs under
    site-packages/nvidia/*/lib, which the loader does not search by default --
    without this the CUDA provider fails with `libcublasLt.so.12: cannot open
    shared object file` and silently falls back to CPU. `preload_dlls()` (ORT
    >= 1.21) adds those dirs. Best-effort and run once per process.
    """
    global _dlls_preloaded
    if _dlls_preloaded:
        return
    _dlls_preloaded = True
    preload = getattr(ort, "preload_dlls", None)
    if preload is None:  # older onnxruntime; rely on the system loader / LD_LIBRARY_PATH
        return
    try:
        preload()
    except Exception as exc:  # noqa: BLE001 - never fatal; CUDA will just fall back to CPU
        logger.warning("onnxruntime.preload_dlls() failed (%s); CUDA libs may not load", exc)


def create_session(
    model_path: Path | str,
    device: Literal["auto", "cpu", "cuda"] = "auto",
    intra_op_threads: int | None = None,
    device_id: int = 0,
):
    """Create an ort.InferenceSession honoring settings.inference.

    CPU is always the fallback. When device is 'cuda'/'auto' and the CUDA
    provider is compiled into the installed onnxruntime, CUDA is tried first
    (pinned to `device_id`); if the CUDA session fails to initialize -- a
    provider that's present but unusable, e.g. missing cuDNN or an out-of-memory
    GPU -- we log a warning and fall back to CPU rather than aborting the run.
    An explicit device='cuda' with no CUDA provider at all also warns.
    """
    import logging

    import onnxruntime as ort

    logger = logging.getLogger(__name__)
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = intra_op_threads or physical_cores()
    # Our detectors pad each image to a multiple of 32, so every distinct photo
    # dimension is a unique input shape. The CPU BFC arena extends on each new
    # shape and never returns memory to the OS, so RSS grows monotonically and
    # eventually a conv fails to allocate its activation buffer (~hundreds of MB)
    # thousands of photos into a run. Disabling the arena frees each allocation
    # after use -- slightly slower, but this is a batch/offline CPU pipeline where
    # runtime is not a constraint. (CUDA gets the equivalent fix via arena opts
    # below.)
    opts.enable_cpu_mem_arena = False
    # Silence per-inference "Expected shape ... does not match actual shape"
    # WARNINGs: our detectors export static output shapes but run on
    # variable-sized (padded) inputs, so VerifyOutputSizes flags every call.
    # The outputs are correct; keep Error/Fatal (3) visible.
    opts.log_severity_level = 3

    cuda_available = "CUDAExecutionProvider" in ort.get_available_providers()
    if device in ("auto", "cuda") and cuda_available:
        _preload_cuda_dlls(ort, logger)
        # Our detectors feed variable-sized (padded to /32) inputs, so every
        # distinct photo dimension is a new input shape. With the CUDA provider's
        # default arena strategy (kNextPowerOfTwo, which never releases memory)
        # plus per-shape cuDNN conv workspaces, GPU memory grows monotonically
        # and OOMs after a few thousand photos. These options bound that growth:
        #   * kSameAsRequested extends the arena by exactly what's asked rather
        #     than doubling, so unique shapes don't compound waste.
        #   * cudnn_conv_use_max_workspace=0 stops each conv reserving max scratch.
        #   * HEURISTIC algo search avoids the default EXHAUSTIVE per-shape search
        #     (slow to warm up and allocates large search buffers per new shape).
        cuda_opts = {
            "device_id": device_id,
            "arena_extend_strategy": "kSameAsRequested",
            "cudnn_conv_use_max_workspace": "0",
            "cudnn_conv_algo_search": "HEURISTIC",
        }
        providers = [
            ("CUDAExecutionProvider", cuda_opts),
            "CPUExecutionProvider",
        ]
        try:
            return ort.InferenceSession(str(model_path), opts, providers=providers)
        except Exception as exc:  # noqa: BLE001 - degrade to CPU on any CUDA init failure
            logger.warning(
                "CUDAExecutionProvider present but session init failed (%s); using CPU", exc
            )
    elif device == "cuda":
        logger.warning(
            "inference.device='cuda' but CUDAExecutionProvider unavailable; using CPU"
        )

    return ort.InferenceSession(str(model_path), opts, providers=["CPUExecutionProvider"])
