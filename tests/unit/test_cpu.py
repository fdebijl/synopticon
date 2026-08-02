"""Core counting must reflect the cgroup, not the host.

``os.cpu_count()`` and ``/proc/cpuinfo`` both enumerate the host's CPUs from
inside a container — no namespace hides them. Sizing a thread pool off either
oversubscribes the cgroup by however much its quota narrowed things, and the
resulting run queue is what makes a co-resident web server time out.
"""

from __future__ import annotations

import synopticon.cpu as cpu


def _fake_files(monkeypatch, mapping: dict[str, str]):
    real_open = open

    def fake_open(path, *a, **kw):
        if str(path) in mapping:
            import io

            return io.StringIO(mapping[str(path)])
        if str(path).startswith("/sys/fs/cgroup"):
            raise FileNotFoundError(path)
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", fake_open)


def test_cgroup_v2_quota_is_honoured(monkeypatch):
    _fake_files(monkeypatch, {cpu._CGROUP_V2: "200000 100000\n"})
    assert cpu.cgroup_cpu_limit() == 2.0


def test_cgroup_v2_unlimited(monkeypatch):
    _fake_files(monkeypatch, {cpu._CGROUP_V2: "max 100000\n"})
    assert cpu.cgroup_cpu_limit() is None


def test_cgroup_v1_fallback(monkeypatch):
    _fake_files(
        monkeypatch,
        {cpu._CGROUP_V1_QUOTA: "150000\n", cpu._CGROUP_V1_PERIOD: "100000\n"},
    )
    assert cpu.cgroup_cpu_limit() == 1.5


def test_cgroup_v1_unlimited_quota(monkeypatch):
    _fake_files(
        monkeypatch,
        {cpu._CGROUP_V1_QUOTA: "-1\n", cpu._CGROUP_V1_PERIOD: "100000\n"},
    )
    assert cpu.cgroup_cpu_limit() is None


def test_available_cores_capped_by_quota(monkeypatch):
    monkeypatch.setattr(cpu.os, "sched_getaffinity", lambda _pid: set(range(32)))
    monkeypatch.setattr(cpu, "cgroup_cpu_limit", lambda: 2.0)
    assert cpu.available_cores() == 2


def test_available_cores_uses_affinity_when_unquotaed(monkeypatch):
    monkeypatch.setattr(cpu.os, "sched_getaffinity", lambda _pid: {0, 1, 2, 3})
    monkeypatch.setattr(cpu, "cgroup_cpu_limit", lambda: None)
    assert cpu.available_cores() == 4


def test_available_cores_never_zero(monkeypatch):
    monkeypatch.setattr(cpu.os, "sched_getaffinity", lambda _pid: set(range(8)))
    monkeypatch.setattr(cpu, "cgroup_cpu_limit", lambda: 0.5)
    assert cpu.available_cores() == 1


def test_physical_cores_capped_by_available(monkeypatch):
    """A 64-thread /proc/cpuinfo inside a 2-core cgroup still means 2."""
    monkeypatch.setattr(cpu, "available_cores", lambda: 2)
    assert cpu.physical_cores() == 2


def test_physical_cores_is_sane_on_this_machine():
    assert 1 <= cpu.physical_cores() <= cpu.available_cores()


def test_job_thread_cap_defaults_to_available_cores(monkeypatch, tmp_path):
    """JobManager reserves a core for the server, from the *container's* count."""
    import synopticon.web.jobs as jobs

    monkeypatch.setattr(jobs, "available_cores", lambda: 4)
    jm = jobs.JobManager(tmp_path / "jobs")
    try:
        assert jm._thread_cap == 3
    finally:
        jm.shutdown()
