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
