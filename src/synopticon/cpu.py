"""CPU topology detection that is honest inside a container.

Dependency-free leaf module: importable from ``cluster/`` and from the web
process without dragging in the image stack.

The naive answers (``os.cpu_count()``, ``/proc/cpuinfo``) both report the
*host's* CPUs from inside a container — neither namespace hides them. A job
sized off those numbers oversubscribes its cgroup by however much the quota
narrowed it, and the resulting run queue is what makes an unrelated request in
the same container time out.
"""

from __future__ import annotations

import os

_CGROUP_V2 = "/sys/fs/cgroup/cpu.max"
_CGROUP_V1_QUOTA = "/sys/fs/cgroup/cpu/cpu.cfs_quota_us"
_CGROUP_V1_PERIOD = "/sys/fs/cgroup/cpu/cpu.cfs_period_us"


def _read_int(path: str) -> int | None:
    try:
        with open(path) as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def cgroup_cpu_limit() -> float | None:
    """CPU quota in cores from the cgroup (v2 then v1), or None if unlimited.

    This is what `docker run --cpus=N` / Swarm's `--limit-cpu` actually set.
    """
    try:
        with open(_CGROUP_V2) as fh:
            quota_s, period_s = fh.read().split()
        if quota_s != "max":
            quota, period = int(quota_s), int(period_s)
            if quota > 0 and period > 0:
                return quota / period
        return None
    except (OSError, ValueError):
        pass

    quota = _read_int(_CGROUP_V1_QUOTA)
    period = _read_int(_CGROUP_V1_PERIOD)
    if quota and quota > 0 and period and period > 0:
        return quota / period
    return None


def available_cores() -> int:
    """Cores this process may actually use: affinity mask ∩ cgroup quota.

    Always >= 1. Prefer this over ``os.cpu_count()`` anywhere a thread pool is
    being sized — under a cgroup quota the extra threads buy no throughput and
    cost a run queue that everything else in the container waits behind.
    """
    try:
        count = len(os.sched_getaffinity(0))
    except AttributeError:  # non-Linux
        count = os.cpu_count() or 1

    limit = cgroup_cpu_limit()
    if limit is not None:
        count = min(count, max(1, int(limit)))
    return max(1, count)


def physical_cores() -> int:
    """Best-effort physical core count, capped by what we may actually use.

    Hyperthreads rarely help compute-bound ONNX ops, hence the physical count;
    the ``available_cores()`` cap is what keeps the answer sane in a container,
    where ``/proc/cpuinfo`` still enumerates the whole host.
    """
    cores: set[tuple[str, str]] = set()
    try:
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
    except OSError:
        pass

    count = len(cores) if cores else (os.cpu_count() or 1)
    return max(1, min(count, available_cores()))
