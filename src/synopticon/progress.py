"""Structured progress protocol: append-only JSONL event stream.

This is a stdlib-only *leaf* module (no synopticon imports), so any layer —
including ``cluster/``, which may never import ``syno/`` or ``pipeline/`` — can
emit progress without violating a module boundary.

Transport
---------
Events are appended, one JSON object per line, to the file named by the
``SYNOPTICON_PROGRESS_FILE`` environment variable. When that variable is unset
the emitter is a no-op and terminal UX is byte-identical to a run without this
module. The file *is* the persistent job log a web job manager tails; stdout /
stderr stay human-only.

Event schema (v1) — consumers ignore unknown fields::

    {"v":1,"ts":...,"event":"phase","phase":"sync.items","space":"personal"}
    {"v":1,"ts":...,"event":"progress","phase":"extract","done":8412,"total":12290}
    {"v":1,"ts":...,"event":"log","level":"warning","message":"...","phase":"sync.faces"}
    {"v":1,"ts":...,"event":"result","ok":true,"stats":{"photos_processed":412}}
    {"v":1,"ts":...,"event":"error","message":"...","traceback":"..."}

Atomicity assumption
--------------------
The handle is opened once in append mode (``O_APPEND``); on Linux each
``write()`` of a single short line is atomic with respect to the file offset,
so two writers appending to the same file (e.g. ``models download`` spawning
``scripts/download_models.py``, which inherits the env var) never interleave a
line. This holds only for **short** lines: keep each emitted line ≲4 KB — the
events here (small dicts of ids / counts) are well under that, and callers
should not stuff large blobs into ``stats``/``extra``.

Robustness
----------
Emitting never raises: a full disk or an unwritable path must not abort an
extract run, so every write is wrapped and errors are swallowed silently.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, TextIO

ENV_VAR = "SYNOPTICON_PROGRESS_FILE"
SCHEMA_VERSION = 1

# Minimum wall-clock gap between two `progress` events for the *same* phase.
# A `done == total` event is always emitted regardless (so the final state is
# never throttled away).
_MIN_PROGRESS_INTERVAL = 0.1  # seconds


class Emitter:
    """Appends v1 JSONL events to ``path``; a no-op when ``path`` is falsy.

    Instances are cheap and safe to hold for the life of a command. All emit
    methods swallow I/O errors so a broken sink can never crash the pipeline.
    """

    def __init__(self, path: str | os.PathLike[str] | None):
        self._fh: TextIO | None = None
        self._last: dict[str, float] = {}
        if not path:
            return
        try:
            # Line-buffered (buffering=1) text append: O_APPEND gives atomic
            # per-line appends on Linux; buffering=1 flushes on each newline.
            self._fh = open(path, "a", buffering=1, encoding="utf-8")
        except OSError:
            self._fh = None  # unwritable sink -> silently degrade to no-op

    @property
    def enabled(self) -> bool:
        return self._fh is not None

    # -- emit primitives ---------------------------------------------------
    def _emit(self, obj: dict[str, Any]) -> None:
        fh = self._fh
        if fh is None:
            return
        try:
            record = {"v": SCHEMA_VERSION, "ts": time.time(), **obj}
            fh.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")
            fh.flush()
        except Exception:  # noqa: BLE001 - a broken sink must never abort a run
            pass

    def phase(self, phase: str, **extra: Any) -> None:
        if self._fh is None:
            return
        self._emit({"event": "phase", "phase": phase, **extra})

    def progress(self, phase: str, done: int, total: int | None, **extra: Any) -> None:
        """Emit a progress event, throttled to <=1 per phase per 100 ms.

        A terminal event (``total`` known and ``done >= total``) is always
        emitted, bypassing the throttle, so consumers see the phase finish.
        """
        if self._fh is None:
            return
        is_final = total is not None and done >= total
        if not is_final:
            now = time.monotonic()
            if now - self._last.get(phase, 0.0) < _MIN_PROGRESS_INTERVAL:
                return
            self._last[phase] = now
        else:
            self._last[phase] = time.monotonic()
        self._emit({"event": "progress", "phase": phase, "done": done, "total": total, **extra})

    def log(self, level: str, message: str, phase: str | None = None, **extra: Any) -> None:
        if self._fh is None:
            return
        obj: dict[str, Any] = {"event": "log", "level": level, "message": message}
        if phase is not None:
            obj["phase"] = phase
        obj.update(extra)
        self._emit(obj)

    def result(self, stats: Any | None = None, ok: bool = True, **extra: Any) -> None:
        if self._fh is None:
            return
        obj: dict[str, Any] = {"event": "result", "ok": ok}
        if stats is not None:
            obj["stats"] = stats
        obj.update(extra)
        self._emit(obj)

    def error(self, message: str, traceback: str | None = None, **extra: Any) -> None:
        if self._fh is None:
            return
        obj: dict[str, Any] = {"event": "error", "message": message}
        if traceback is not None:
            obj["traceback"] = traceback
        obj.update(extra)
        self._emit(obj)


class _NoopEmitter(Emitter):
    """An Emitter that is permanently disabled (env var unset)."""

    def __init__(self) -> None:
        super().__init__(None)


# get_emitter() caches on the env var value: reading a distinct value builds a
# fresh emitter, so a test toggling SYNOPTICON_PROGRESS_FILE gets the right one
# without a manual reset, while repeated calls within one run share a handle.
_cache: tuple[str | None, Emitter] | None = None


def get_emitter() -> Emitter:
    """Return the process emitter, cached on ``SYNOPTICON_PROGRESS_FILE``.

    Returns a no-op emitter when the env var is unset or empty.
    """
    global _cache
    path = os.environ.get(ENV_VAR) or None
    if _cache is not None and _cache[0] == path:
        return _cache[1]
    emitter: Emitter = _NoopEmitter() if path is None else Emitter(path)
    _cache = (path, emitter)
    return emitter


class EventLogHandler(logging.Handler):
    """A ``logging.Handler`` that forwards records as ``log`` progress events.

    Attached to a dedicated logger (e.g. ``synopticon.apply``), it bridges that
    logger's records into the JSONL stream with zero call-site changes. The
    target emitter is resolved lazily via :func:`get_emitter` unless one is
    passed explicitly, so it honours the env var in effect when a record fires.
    """

    def __init__(self, emitter: Emitter | None = None, phase: str | None = None):
        super().__init__()
        self._emitter = emitter
        self._phase = phase

    def emit(self, record: logging.LogRecord) -> None:
        emitter = self._emitter or get_emitter()
        try:
            emitter.log(record.levelname.lower(), record.getMessage(), phase=self._phase)
        except Exception:  # noqa: BLE001 - logging must never raise
            pass
