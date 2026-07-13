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


def create_session(
    model_path: Path | str,
    device: Literal["auto", "cpu", "cuda"] = "auto",
    intra_op_threads: int | None = None,
):
    """Create an ort.InferenceSession honoring settings.inference.

    CPUExecutionProvider always present; CUDAExecutionProvider is put first
    when requested/available (device='cuda' or 'auto').
    """
    import onnxruntime as ort

    opts = ort.SessionOptions()
    opts.intra_op_num_threads = intra_op_threads or physical_cores()

    providers = ["CPUExecutionProvider"]
    if device in ("auto", "cuda") and "CUDAExecutionProvider" in ort.get_available_providers():
        providers.insert(0, "CUDAExecutionProvider")
    elif device == "cuda":
        # Explicit cuda request without the provider: fall through to CPU with a warning.
        import logging

        logging.getLogger(__name__).warning(
            "inference.device='cuda' but CUDAExecutionProvider unavailable; using CPU"
        )
    return ort.InferenceSession(str(model_path), opts, providers=providers)
