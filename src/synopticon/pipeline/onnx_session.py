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

    cuda_available = "CUDAExecutionProvider" in ort.get_available_providers()
    if device in ("auto", "cuda") and cuda_available:
        _preload_cuda_dlls(ort, logger)
        providers = [
            ("CUDAExecutionProvider", {"device_id": device_id}),
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
