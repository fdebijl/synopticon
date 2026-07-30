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

Continuity
----------
Two mechanisms keep a long phase from going silent, so a job log is never a blank
box while work is happening:

* :meth:`Emitter.progress` narrates itself into the ``log`` stream every
  ``_HEARTBEAT_INTERVAL`` seconds with a rate and an ETA, and once more when the
  phase completes (with the elapsed time). One line per phase per interval, so
  the history stays readable over a multi-hour run.
* :func:`install_log_bridge` forwards the whole ``synopticon`` logger tree into
  the stream as ``log`` events, so every module's existing ``log.info`` /
  ``log.warning`` commentary reaches a consumer with no call-site changes.

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

# How often a long-running phase narrates itself into the *log* stream as well.
# `progress` events drive a live bar that only ever shows the current value;
# a periodic log line leaves a scrollable history ("what was it doing 4 minutes
# ago, and was it this slow then?"). One line per phase per interval, plus one
# closing line when the phase completes.
_HEARTBEAT_INTERVAL = 30.0  # seconds

#: Traceback budget per `error` event, to honour the ≲4 KB per-line assumption.
_MAX_TRACEBACK_CHARS = 3000


def format_duration(seconds: float) -> str:
    """Compact human duration: ``42s``, ``4m 12s``, ``1h 07m``."""
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m {int(seconds % 60):02d}s"
    return f"{int(seconds // 3600)}h {int(seconds % 3600) // 60:02d}m"


def format_rate(per_second: float) -> str:
    """Rate with a sensible unit: ``41.2/s`` for fast work, ``2.4/min`` for slow."""
    if per_second >= 1.0:
        return f"{per_second:.1f}/s"
    if per_second > 0:
        return f"{per_second * 60:.1f}/min"
    return "0/s"


class Emitter:
    """Appends v1 JSONL events to ``path``; a no-op when ``path`` is falsy.

    Instances are cheap and safe to hold for the life of a command. All emit
    methods swallow I/O errors so a broken sink can never crash the pipeline.
    """

    def __init__(self, path: str | os.PathLike[str] | None):
        self._fh: TextIO | None = None
        self._last: dict[str, float] = {}
        # Per-phase heartbeat bookkeeping. `_started` is when the phase began
        # (from `phase()`, so it covers setup before the first progress event and
        # gives an honest total elapsed); `_origin` is the rate baseline
        # (monotonic, done) taken at the first progress event, so a slow startup
        # does not permanently depress the reported throughput. `_beat` throttles
        # the narration itself.
        self._started: dict[str, float] = {}
        self._origin: dict[str, tuple[float, int]] = {}
        self._beat: dict[str, float] = {}
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
        # A phase (re)start resets its timing: the same phase name is reused
        # across spaces, and carrying the previous space's baseline over would
        # report a nonsense rate and elapsed for the new one.
        now = time.monotonic()
        self._started[phase] = now
        self._origin.pop(phase, None)
        self._beat[phase] = now
        self._emit({"event": "phase", "phase": phase, **extra})

    def progress(self, phase: str, done: int, total: int | None, **extra: Any) -> None:
        """Emit a progress event, throttled to <=1 per phase per 100 ms.

        A terminal event (``total`` known and ``done >= total``) is always
        emitted, bypassing the throttle, so consumers see the phase finish.
        Long phases also get a periodic ``log`` heartbeat with rate + ETA, and a
        closing summary line when they complete — see ``_HEARTBEAT_INTERVAL``.
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
        self._heartbeat(phase, done, total, is_final, extra.get("space"))

    def _heartbeat(
        self,
        phase: str,
        done: int,
        total: int | None,
        is_final: bool,
        space: Any = None,
    ) -> None:
        """Narrate a long phase into the log stream at most once per interval."""
        now = time.monotonic()
        origin = self._origin.get(phase)
        if origin is None:
            self._origin[phase] = (now, done)
            self._beat.setdefault(phase, now)
            self._started.setdefault(phase, now)
            return
        rate_t0, first_done = origin
        rate_span = now - rate_t0
        elapsed = now - self._started.get(phase, rate_t0)
        if not is_final and now - self._beat.get(phase, rate_t0) < _HEARTBEAT_INTERVAL:
            return
        self._beat[phase] = now
        where = f"[{space}] " if space else ""
        if is_final:
            # A phase that finished in well under a heartbeat needs no summary —
            # the caller's own log line already says what happened, and "0 done
            # in 0s" is worse than silence.
            if elapsed < 1.0:
                return
            # Overall throughput, not the trailing-window rate: the summary says
            # "N done in T", so the figure beside it has to be N/T. A phase whose
            # cost sits in one opaque step before the counted loop (the kNN
            # matmul, a fetch) otherwise reports a rate that contradicts its own
            # elapsed time by two orders of magnitude.
            msg = f"{where}{phase}: {done} done in {format_duration(elapsed)}"
            if elapsed > 0 and done > 0:
                msg += f" ({format_rate(done / elapsed)})"
        else:
            # Interim lines keep the trailing-window rate, which is what makes the
            # ETA reflect how fast the loop is running *now*.
            rate = (done - first_done) / rate_span if rate_span > 0 else 0.0
            msg = f"{where}{phase}: {done}"
            if total:
                msg += f"/{total} ({done * 100 // total}%)"
            if rate > 0:
                msg += f" · {format_rate(rate)}"
                if total and total > done:
                    eta = (total - done) / rate
                    if eta >= 1.0:  # "ETA 0s" is noise
                        msg += f" · ETA {format_duration(eta)}"
        self.log("info", msg, phase=phase)

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
            # Keep the line inside the ~4 KB the atomic-append guarantee assumes.
            # A deep traceback easily runs past that; the tail is the useful end
            # (innermost frames and the exception itself), so drop from the front.
            if len(traceback) > _MAX_TRACEBACK_CHARS:
                traceback = (
                    "[... earlier frames truncated ...]\n"
                    + traceback[-_MAX_TRACEBACK_CHARS:]
                )
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


def install_log_bridge(logger_name: str = "synopticon", level: int = logging.INFO) -> bool:
    """Forward the ``synopticon`` logger tree into the event stream.

    Every module already narrates itself through ``logging`` (device selection,
    skipped photos, throttling, write-back decisions); with the progress protocol
    enabled those records are exactly the human-readable commentary a GUI job log
    wants, so bridge them wholesale instead of duplicating each call site.

    A no-op returning ``False`` when the protocol is disabled, so terminal
    behaviour is unchanged. Idempotent: a second call adds no second handler.
    """
    emitter = get_emitter()
    if not emitter.enabled:
        return False
    log = logging.getLogger(logger_name)
    if not any(isinstance(h, EventLogHandler) for h in log.handlers):
        log.addHandler(EventLogHandler())
    # Only raise verbosity, never lower a level the caller chose deliberately.
    if log.level == logging.NOTSET or log.level > level:
        log.setLevel(level)
    return True
