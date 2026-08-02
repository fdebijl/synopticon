"""Hardware / environment diagnostics for `synopticon hwinfo`.

Everything here is best-effort and must never raise: the whole point is to run
on a machine that's misbehaving. Optional deps, missing /proc entries, absent
GPUs, and unreadable paths all degrade to a printed "unknown"/"absent" rather
than an exception.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from synopticon.config import Settings
from synopticon.pipeline.onnx_session import physical_cores

Section = tuple[str, list[tuple[str, str]]]


def _human_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TiB"


def _proc_cpuinfo() -> dict[str, str]:
    fields: dict[str, str] = {}
    try:
        with open("/proc/cpuinfo") as fh:
            for line in fh:
                key, sep, val = line.partition(":")
                if sep and key.strip() not in fields:
                    fields[key.strip()] = val.strip()
    except OSError:
        pass
    return fields


def _in_docker() -> bool:
    if Path("/.dockerenv").exists():
        return True
    try:
        return "docker" in Path("/proc/1/cgroup").read_text()
    except OSError:
        return False


def _platform_section() -> Section:
    rows = [
        ("OS", platform.platform()),
        ("Python", f"{platform.python_version()} ({platform.python_implementation()})"),
        ("Executable", sys.executable),
        ("In container", "yes" if _in_docker() else "no"),
    ]
    return ("Platform", rows)


def _usable_cores_row() -> str:
    from synopticon.cpu import available_cores, cgroup_cpu_limit

    limit = cgroup_cpu_limit()
    if limit is None:
        return f"{available_cores()} (no cgroup CPU quota)"
    return f"{available_cores()} (cgroup quota {limit:g} cores)"


def _cpu_section() -> Section:
    info = _proc_cpuinfo()
    model = info.get("model name") or platform.processor() or "unknown"
    logical = os.cpu_count() or 0
    flags = set(info.get("flags", "").split())
    simd = [f for f in ("avx512f", "avx2", "avx", "fma", "sse4_2") if f in flags]
    rows = [
        ("Model", model),
        ("Architecture", platform.machine()),
        ("Physical cores", str(physical_cores())),
        ("Logical cores", str(logical)),
        # Both counts above are host-wide readings; in a container the quota is
        # the number that actually governs, so show it when there is one.
        ("Usable cores", _usable_cores_row()),
        ("SIMD", ", ".join(simd) if simd else "unknown (non-x86 or /proc unavailable)"),
    ]
    return ("CPU", rows)


def _meminfo() -> tuple[int | None, int | None]:
    """(total, available) bytes, best-effort; None where unknown."""
    try:
        vals: dict[str, int] = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                parts = rest.split()
                if parts:
                    vals[key.strip()] = int(parts[0]) * 1024  # kB -> bytes
        return vals.get("MemTotal"), vals.get("MemAvailable")
    except OSError:
        pass
    try:
        total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        return total, None
    except (ValueError, OSError, AttributeError):
        return None, None


def _memory_section() -> Section:
    total, available = _meminfo()
    rows = [("Total RAM", _human_bytes(total) if total else "unknown")]
    if available is not None:
        rows.append(("Available RAM", _human_bytes(available)))
    return ("Memory", rows)


def _nvidia_gpus() -> list[str]:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,driver_version",
             "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def _onnx_section(settings: Settings) -> Section:
    rows: list[tuple[str, str]] = []
    gpus = _nvidia_gpus()
    cuda = False
    ort_ok = False
    try:
        import onnxruntime as ort

        ort_ok = True
        providers = ort.get_available_providers()
        cuda = "CUDAExecutionProvider" in providers
        rows.append(("onnxruntime", ort.__version__))
        rows.append(("Execution providers", ", ".join(providers)))
    except Exception as exc:  # noqa: BLE001 - diagnostics must not crash
        rows.append(("onnxruntime", f"import failed: {exc}"))

    if not ort_ok:
        gpu_status = "unknown — onnxruntime failed to import (see the row above)"
    elif cuda:
        gpu_status = "available (CUDA)"
    elif gpus:
        # The single most common surprise: healthy GPU, but the CPU-only wheel.
        gpu_status = ("not available — GPU detected but CPU-only onnxruntime is installed; "
                      "reinstall with the gpu extra (see README: GPU acceleration)")
    else:
        gpu_status = "not available (CPU only)"
    rows.append(("GPU acceleration", gpu_status))
    rows.append(("NVIDIA GPU(s)", "; ".join(gpus) if gpus else "none detected (nvidia-smi absent or no GPU)"))

    inf = settings.inference
    rows.append(("Configured device", inf.device))
    rows.append(("device_id", str(inf.device_id)))
    rows.append(("intra_op_threads", str(inf.intra_op_threads) if inf.intra_op_threads else f"{physical_cores()} (physical cores)"))
    rows.append(("batch_size", str(inf.batch_size)))
    thread_env = [f"{k}={os.environ[k]}" for k in
                  ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS") if k in os.environ]
    rows.append(("Thread env", ", ".join(thread_env) if thread_env else "unset (defaults)"))
    return ("Inference (ONNX Runtime)", rows)


def _libs_section() -> Section:
    rows: list[tuple[str, str]] = []
    # (display name, import name, optional?)
    libs = [
        ("numpy", "numpy", False),
        ("opencv", "cv2", False),
        ("scikit-learn", "sklearn", False),
        ("scipy", "scipy", False),
        ("Pillow", "PIL", False),
        ("faiss", "faiss", True),
        ("torch", "torch", True),
    ]
    for name, mod, optional in libs:
        try:
            m = __import__(mod)
            rows.append((name, getattr(m, "__version__", "installed (version unknown)")))
        except Exception:  # noqa: BLE001
            rows.append((name, "not installed (optional)" if optional else "NOT INSTALLED"))
    return ("Key libraries", rows)


def _classify(key: str) -> str:
    k = key.lower()
    if "scrfd" in k or "yolo" in k:
        return "detector"
    if any(t in k for t in ("glint", "arcface", "r100", "adaface", "magface", "iresnet")):
        return "embedder"
    return "other"


def _models_section(settings: Settings) -> Section:
    from synopticon.pipeline import manifest

    models_dir = settings.storage.models_dir
    rows: list[tuple[str, str]] = [("models_dir", str(models_dir))]
    try:
        entries = manifest.load_manifest(models_dir)
    except Exception as exc:  # noqa: BLE001
        rows.append(("manifest", f"unreadable: {exc}"))
        return ("Models", rows)

    if not entries:
        rows.append(("manifest", "absent — run: synopticon models download"))
        return ("Models", rows)

    detectors = embedders = 0
    for key in sorted(entries):
        path = models_dir / entries[key].get("file", "")
        role = _classify(key)
        if path.is_file():
            present = f"present ({_human_bytes(path.stat().st_size)}, {role})"
            if role == "detector":
                detectors += 1
            elif role == "embedder":
                embedders += 1
        else:
            present = f"MISSING ({role})"
        rows.append((key, present))

    ok = detectors >= 1 and embedders >= 1
    rows.append((
        "Extraction capability",
        f"OK ({detectors} detector(s), {embedders} embedder(s))" if ok
        else f"INSUFFICIENT (need >=1 detector and >=1 embedder; have {detectors}/{embedders})",
    ))
    return ("Models", rows)


def _disk_row(label: str, path: Path) -> tuple[str, str]:
    try:
        usage = shutil.disk_usage(path if path.exists() else path.anchor or ".")
        return (label, f"{_human_bytes(usage.free)} free of {_human_bytes(usage.total)}")
    except OSError as exc:
        return (label, f"unknown ({exc})")


def _storage_section(settings: Settings) -> Section:
    st = settings.storage
    db = st.db_path
    rows = [
        ("data_dir", str(st.data_dir)),
        ("database", f"{_human_bytes(db.stat().st_size)}" if db.is_file() else "not created yet"),
        ("keep_originals", str(st.keep_originals)),
        ("originals_cache_gb", str(st.originals_cache_gb)),
        _disk_row("data_dir disk", st.data_dir),
        _disk_row("models_dir disk", st.models_dir),
    ]
    return ("Storage", rows)


def collect(settings: Settings) -> list[Section]:
    return [
        _platform_section(),
        _cpu_section(),
        _memory_section(),
        _onnx_section(settings),
        _libs_section(),
        _models_section(settings),
        _storage_section(settings),
    ]


def render(settings: Settings) -> str:
    lines: list[str] = []
    for title, rows in collect(settings):
        lines.append(f"== {title} ==")
        width = max((len(label) for label, _ in rows), default=0)
        for label, value in rows:
            lines.append(f"  {label.ljust(width)}  {value}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
