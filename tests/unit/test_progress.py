"""Structured progress protocol: JSONL validity, no-op, throttle, robustness."""

from __future__ import annotations

import json
import logging

from synopticon import progress
from synopticon.progress import Emitter, EventLogHandler, get_emitter


def _read(path) -> list[dict]:
    """Parse a JSONL event file, asserting exactly one object per line."""
    objs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        objs.append(json.loads(line))  # raises if a line is not a single JSON object
    return objs


def test_emits_valid_jsonl_with_v1_schema(tmp_path):
    path = tmp_path / "events.jsonl"
    em = Emitter(path)
    assert em.enabled

    em.phase("sync.items", space="personal")
    em.progress("extract", 12, 12, space="personal")
    em.log("warning", "skipped photo 123", phase="sync.faces")
    em.result(stats={"photos_processed": 5}, ok=True)
    em.error("boom", traceback="Traceback ...")

    objs = _read(path)
    assert [o["event"] for o in objs] == ["phase", "progress", "log", "result", "error"]
    # v1 stamp + timestamp on every line.
    assert all(o["v"] == 1 for o in objs)
    assert all(isinstance(o["ts"], (int, float)) for o in objs)

    phase, prog, logev, result, error = objs
    assert phase["phase"] == "sync.items" and phase["space"] == "personal"
    assert prog["done"] == 12 and prog["total"] == 12
    assert logev["level"] == "warning" and logev["message"] == "skipped photo 123"
    assert logev["phase"] == "sync.faces"
    assert result["ok"] is True and result["stats"] == {"photos_processed": 5}
    assert error["message"] == "boom" and error["traceback"] == "Traceback ..."


def test_noop_when_env_unset(tmp_path, monkeypatch):
    monkeypatch.delenv(progress.ENV_VAR, raising=False)
    em = get_emitter()
    assert not em.enabled
    # None of these do anything or raise.
    em.phase("x")
    em.progress("x", 1, 2)
    em.log("info", "hi")
    em.result(stats={"a": 1})
    em.error("nope")
    # No file was created anywhere.
    assert list(tmp_path.iterdir()) == []


def test_get_emitter_honours_env(tmp_path, monkeypatch):
    path = tmp_path / "ev.jsonl"
    monkeypatch.setenv(progress.ENV_VAR, str(path))
    em = get_emitter()
    assert em.enabled
    em.phase("sync.items")
    assert _read(path)[0]["event"] == "phase"


def test_progress_throttles_but_always_emits_final(tmp_path):
    path = tmp_path / "events.jsonl"
    em = Emitter(path)
    # Rapid-fire (well under the 100 ms throttle window): only the first
    # non-final event lands; intermediates collapse.
    for done in range(1, 10):
        em.progress("extract", done, 10)
    # The terminal event bypasses the throttle and is always emitted.
    em.progress("extract", 10, 10)

    progs = [o for o in _read(path) if o["event"] == "progress"]
    assert len(progs) < 10  # throttling happened
    assert progs[-1]["done"] == 10 and progs[-1]["total"] == 10  # final always present


def test_write_errors_are_swallowed(tmp_path):
    # Parent dir does not exist -> open() fails -> emitter degrades to no-op.
    bad = tmp_path / "missing_dir" / "events.jsonl"
    em = Emitter(bad)
    assert not em.enabled
    # Emitting must not raise despite the broken sink.
    em.phase("x")
    em.progress("x", 1, 1)
    em.result(stats={"a": 1})
    assert not bad.exists()


def test_long_phase_narrates_itself_with_rate_and_eta(tmp_path, monkeypatch):
    """A slow phase must not go silent between the bar's updates."""
    monkeypatch.setattr(progress, "_HEARTBEAT_INTERVAL", 0.0)  # beat every event
    monkeypatch.setattr(progress, "_MIN_PROGRESS_INTERVAL", 0.0)  # no throttling
    path = tmp_path / "events.jsonl"
    em = Emitter(path)

    clock = [1000.0]
    monkeypatch.setattr(progress.time, "monotonic", lambda: clock[0])
    em.phase("extract", space="personal")
    for done in (100, 200, 300):
        clock[0] += 10.0  # 10 items/s
        em.progress("extract", done, 400, space="personal")

    beats = [o["message"] for o in _read(path) if o["event"] == "log"]
    # First progress event only establishes the rate baseline; the rest narrate.
    assert len(beats) == 2
    assert beats[-1] == "[personal] extract: 300/400 (75%) · 10.0/s · ETA 10s"


def test_phase_completion_summary_reports_elapsed(tmp_path, monkeypatch):
    monkeypatch.setattr(progress, "_HEARTBEAT_INTERVAL", 1e9)  # only the closing line
    path = tmp_path / "events.jsonl"
    em = Emitter(path)

    clock = [1000.0]  # nonzero: monotonic() == 0 would trip the throttle default
    monkeypatch.setattr(progress.time, "monotonic", lambda: clock[0])
    em.phase("extract")
    em.progress("extract", 0, 120)
    clock[0] += 60.0
    em.progress("extract", 120, 120)

    beats = [o["message"] for o in _read(path) if o["event"] == "log"]
    assert beats == ["extract: 120 done in 1m 00s (2.0/s)"]


def test_completion_rate_matches_its_own_elapsed_after_slow_setup(tmp_path, monkeypatch):
    """A phase whose cost is one opaque step before the counted loop.

    `cluster.graph` spends ~50 s in the kNN matmul, then races through a fast
    per-row loop. The trailing-window rate is huge and right for the ETA, but a
    summary reading "N done in 50s (217393/s)" contradicts itself — so the
    closing line reports overall throughput instead.
    """
    monkeypatch.setattr(progress, "_HEARTBEAT_INTERVAL", 1e9)
    monkeypatch.setattr(progress, "_MIN_PROGRESS_INTERVAL", 0.0)
    path = tmp_path / "events.jsonl"
    em = Emitter(path)
    clock = [1000.0]
    monkeypatch.setattr(progress.time, "monotonic", lambda: clock[0])

    em.phase("cluster.graph")
    clock[0] += 50.0  # the matmul: no progress events during it
    em.progress("cluster.graph", 0, 50_000)
    clock[0] += 0.5  # the fast post-loop
    em.progress("cluster.graph", 50_000, 50_000)

    beats = [o["message"] for o in _read(path) if o["event"] == "log"]
    # 50000 / 50.5 s ~= 990/s, not the 100000/s of the trailing window.
    assert beats == ["cluster.graph: 50000 done in 50s (990.1/s)"]


def test_sub_second_eta_is_omitted(tmp_path, monkeypatch):
    monkeypatch.setattr(progress, "_HEARTBEAT_INTERVAL", 0.0)
    monkeypatch.setattr(progress, "_MIN_PROGRESS_INTERVAL", 0.0)
    path = tmp_path / "events.jsonl"
    em = Emitter(path)
    clock = [1000.0]
    monkeypatch.setattr(progress.time, "monotonic", lambda: clock[0])
    em.phase("cluster.graph")
    em.progress("cluster.graph", 0, 1000)
    clock[0] += 1.0  # 999 items in 1 s -> under a second remaining
    em.progress("cluster.graph", 999, 1000)
    beats = [o["message"] for o in _read(path) if o["event"] == "log"]
    assert beats == ["cluster.graph: 999/1000 (99%) · 999.0/s"]


def test_fast_phase_emits_no_completion_noise(tmp_path, monkeypatch):
    """"0 done in 0s" is worse than silence — sub-second phases stay quiet."""
    monkeypatch.setattr(progress, "_HEARTBEAT_INTERVAL", 0.0)
    path = tmp_path / "events.jsonl"
    em = Emitter(path)
    em.phase("cluster.persist")
    em.progress("cluster.persist", 0, 3)
    em.progress("cluster.persist", 3, 3)
    assert [o for o in _read(path) if o["event"] == "log"] == []


def test_phase_restart_resets_the_rate_baseline(tmp_path, monkeypatch):
    """The same phase name is reused per space; timings must not carry over."""
    monkeypatch.setattr(progress, "_HEARTBEAT_INTERVAL", 0.0)
    monkeypatch.setattr(progress, "_MIN_PROGRESS_INTERVAL", 0.0)
    path = tmp_path / "events.jsonl"
    em = Emitter(path)
    clock = [1000.0]
    monkeypatch.setattr(progress.time, "monotonic", lambda: clock[0])

    em.phase("sync.faces", space="personal")
    em.progress("sync.faces", 0, 100, space="personal")
    clock[0] += 100.0  # a slow first space: 1/s
    em.progress("sync.faces", 100, 100, space="personal")

    em.phase("sync.faces", space="shared")  # restart
    em.progress("sync.faces", 0, 100, space="shared")
    clock[0] += 10.0  # a fast second space: 10/s
    em.progress("sync.faces", 100, 100, space="shared")

    beats = [o["message"] for o in _read(path) if o["event"] == "log"]
    assert beats[0] == "[personal] sync.faces: 100 done in 1m 40s (1.0/s)"
    assert beats[1] == "[shared] sync.faces: 100 done in 10s (10.0/s)"


def test_long_traceback_is_truncated_to_keep_the_line_atomic(tmp_path):
    """Lines must stay ≲4 KB, or two appending writers can interleave."""
    path = tmp_path / "events.jsonl"
    em = Emitter(path)
    deep = "\n".join(f'  File "mod{i}.py", line {i}, in fn{i}' for i in range(400))
    em.error("RuntimeError: boom", traceback=deep + "\nRuntimeError: boom")

    line = path.read_text(encoding="utf-8").splitlines()[0]
    assert len(line.encode()) < 4096
    tb = json.loads(line)["traceback"]
    assert tb.startswith("[... earlier frames truncated ...]")
    # The tail — the part that names the failure — survives.
    assert tb.endswith("RuntimeError: boom")


def test_short_traceback_is_passed_through_verbatim(tmp_path):
    path = tmp_path / "events.jsonl"
    em = Emitter(path)
    em.error("boom", traceback="Traceback ...\nValueError: boom")
    assert json.loads(path.read_text().splitlines()[0])["traceback"] == (
        "Traceback ...\nValueError: boom"
    )


def test_log_bridge_forwards_module_logging(tmp_path, monkeypatch):
    monkeypatch.setenv(progress.ENV_VAR, str(tmp_path / "ev.jsonl"))
    logger = logging.getLogger("synopticon")
    saved = list(logger.handlers), logger.level
    try:
        logger.handlers.clear()
        assert progress.install_log_bridge() is True
        # Idempotent: a second call must not double up handlers (or lines).
        assert progress.install_log_bridge() is True
        assert sum(isinstance(h, EventLogHandler) for h in logger.handlers) == 1

        logging.getLogger("synopticon.pipeline.runner").info("extract: running on CPU")
        logs = [o for o in _read(tmp_path / "ev.jsonl") if o["event"] == "log"]
        assert [(o["level"], o["message"]) for o in logs] == [
            ("info", "extract: running on CPU")
        ]
    finally:
        logger.handlers[:] = saved[0]
        logger.setLevel(saved[1])


def test_log_bridge_is_a_noop_without_the_env_var(monkeypatch):
    monkeypatch.delenv(progress.ENV_VAR, raising=False)
    logger = logging.getLogger("synopticon")
    before = list(logger.handlers)
    assert progress.install_log_bridge() is False
    assert logger.handlers == before


def test_event_log_handler_converts_records(tmp_path):
    path = tmp_path / "events.jsonl"
    em = Emitter(path)
    logger = logging.getLogger("synopticon.test.bridge")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(EventLogHandler(em, phase="apply"))

    logger.info("assign ok item=1")
    logger.warning("write failed code=402")

    logs = [o for o in _read(path) if o["event"] == "log"]
    assert [(o["level"], o["message"], o["phase"]) for o in logs] == [
        ("info", "assign ok item=1", "apply"),
        ("warning", "write failed code=402", "apply"),
    ]
