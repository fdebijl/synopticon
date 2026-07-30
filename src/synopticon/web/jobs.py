"""Subprocess job manager for the Synopticon web GUI.

Runs allowlisted CLI commands as serialized `python -m synopticon` subprocesses,
tails their structured progress events, and enforces the apply/destructive
consent model server-side. Pure stdlib + existing deps only — this module must
import cleanly without fastapi installed (the web app wires it up).

Design (see the web-GUI plan §2 and §6):

* Commands are never taken as raw argv. `JOB_SPECS` maps a job name to a
  `JobSpec` whose `build_argv(params)` whitelists each parameter. The consent
  layer (`validate_consent`) is what decides when `--apply*`/`-y` flags are
  appended — the GUI never reaches an interactive `typer.confirm`, and the
  safety gates are never silently weakened. `apply-all` and `-Y` are never
  emitted from here.
* One worker thread drains a FIFO queue (max 5 in-flight jobs). Each job runs
  in its own process group (`start_new_session=True`); stdout/stderr are
  redirected to files; a 250 ms tailer thread follows `events.jsonl` into an
  in-memory ring buffer with a monotonic `seq` cursor and a latest-per-phase
  snapshot. `job.json` metadata is rewritten on every state change.
* The tailer follows `stdout.log` / `stderr.log` too, mirroring them as `log`
  events (tagged with `stream`, ANSI-stripped, `\r` redraws collapsed to their
  final frame). Without this a command that dies before emitting any structured
  event — bad config, missing weights, an uncaught traceback — leaves the GUI
  with an empty log and no cause. A non-zero exit that emitted no `error` event
  additionally gets one synthesized from the tail of stderr.
* On startup, jobs left `running` by a crashed server are re-adopted if their
  pid is still a live `synopticon` process, else marked `interrupted`.
"""

from __future__ import annotations

import enum
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

# --------------------------------------------------------------------------- #
# Errors                                                                        #
# --------------------------------------------------------------------------- #


class JobError(Exception):
    """Base class for job-manager errors."""


class JobParamError(JobError):
    """A submitted parameter was missing, of the wrong type, or not allowed.

    The web layer maps this to HTTP 422.
    """


class QueueFullError(JobError):
    """The FIFO queue already holds the maximum number of in-flight jobs.

    The web layer maps this to HTTP 409.
    """


class ConsentError(JobError):
    """A destructive action was requested without the required consent.

    Carries *which* consent input is missing so the UI can prompt for it, but
    deliberately never carries the expected phrase text itself — a client must
    already know the phrase to satisfy the gate. The web layer maps this to
    HTTP 428 (Precondition Required).
    """

    def __init__(self, requirement: str, field: str, detail: str | None = None):
        #: one of "confirm" | "flag" | "phrase"
        self.requirement = requirement
        #: the request field that must be supplied, e.g. "confirm",
        #: "apply_merges", "confirm_phrase"
        self.field = field
        #: a stable machine identifier for the gate (e.g. "merge_named",
        #: "reset_all") — NEVER the phrase a user must type.
        self.detail = detail
        msg = f"consent required: {requirement} '{field}'"
        if detail:
            msg += f" ({detail})"
        super().__init__(msg)


# --------------------------------------------------------------------------- #
# Job specs (allowlist)                                                         #
# --------------------------------------------------------------------------- #


class DangerLevel(enum.Enum):
    """How much consent the most dangerous form of a command needs.

    Advisory metadata for the UI; the authoritative rules live in
    ``validate_consent``.
    """

    SAFE = "safe"
    CONFIRM = "confirm"
    TYPED_PHRASE = "typed_phrase"


@dataclass(frozen=True)
class JobSpec:
    """One allowlisted command: name, argv builder, and max danger level."""

    name: str
    build_argv: Callable[[dict], list[str]]
    danger: DangerLevel = DangerLevel.SAFE


# -- param coercion helpers -------------------------------------------------- #


def _get(params: dict, key: str) -> Any:
    return params.get(key) if isinstance(params, dict) else None


def _bool(params: dict, key: str, default: bool = False) -> bool:
    val = _get(params, key)
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes", "on")
    raise JobParamError(f"{key!r} must be a boolean")


def _int(params: dict, key: str) -> int | None:
    val = _get(params, key)
    if val is None or val == "":
        return None
    if isinstance(val, bool):
        raise JobParamError(f"{key!r} must be an integer")
    try:
        return int(val)
    except (TypeError, ValueError):
        raise JobParamError(f"{key!r} must be an integer")


def _str(params: dict, key: str) -> str | None:
    val = _get(params, key)
    if val is None:
        return None
    if not isinstance(val, str):
        raise JobParamError(f"{key!r} must be a string")
    val = val.strip()
    return val or None


def _str_list(params: dict, key: str) -> list[str]:
    val = _get(params, key)
    if val is None:
        return []
    if isinstance(val, str):
        parts: Iterable[str] = val.split(",")
    elif isinstance(val, (list, tuple)):
        parts = val
    else:
        raise JobParamError(f"{key!r} must be a string or list of strings")
    out: list[str] = []
    for p in parts:
        if not isinstance(p, str):
            raise JobParamError(f"{key!r} entries must be strings")
        p = p.strip()
        if p:
            out.append(p)
    return out


# -- individual argv builders ------------------------------------------------ #

_APPLY_KINDS = frozenset(
    {"assign", "low_confidence", "reassign", "merge", "merge_named", "new_person"}
)
_RECLUSTER_SECTIONS = ("clustering", "crossref")


def _apply_kinds(params: dict) -> list[str]:
    """Validated apply kinds (may be empty → CLI default assign,low_confidence)."""
    kinds = _str_list(params, "kinds")
    bad = [k for k in kinds if k not in _APPLY_KINDS]
    if bad:
        raise JobParamError(f"unknown apply kind(s): {', '.join(sorted(set(bad)))}")
    return kinds


def _build_sync(params: dict) -> list[str]:
    argv = ["sync"]
    space = _str(params, "space")
    if space:
        argv += ["--space", space]
    if _bool(params, "hash"):
        argv.append("--hash")
    if _bool(params, "skip_faces"):
        argv.append("--skip-faces")
    if _bool(params, "all_faces"):
        argv.append("--all-faces")
    # resume defaults to True on the CLI; only emit the flag to turn it off.
    if _get(params, "resume") is not None and not _bool(params, "resume", True):
        argv.append("--no-resume")
    return argv


def _build_extract(params: dict) -> list[str]:
    argv = ["extract"]
    limit = _int(params, "limit")
    if limit is not None:
        argv += ["--limit", str(limit)]
    photo_id = _int(params, "photo_id")
    if photo_id is not None:
        argv += ["--photo-id", str(photo_id)]
    space = _str(params, "space")
    if space:
        argv += ["--space", space]
    return argv


def _build_cluster(params: dict) -> list[str]:
    return ["cluster"]


def _build_recluster(params: dict) -> list[str]:
    """recluster with overrides whitelisted to clustering.*/crossref.* keys."""
    argv = ["recluster"]
    overrides = _get(params, "overrides") or {}
    if not isinstance(overrides, dict):
        raise JobParamError("'overrides' must be an object of dotted keys to values")
    for key, value in overrides.items():
        if not isinstance(key, str) or "." not in key:
            raise JobParamError(f"override key must be 'section.key', got {key!r}")
        section, _, subkey = key.partition(".")
        if section not in _RECLUSTER_SECTIONS or not subkey or "." in subkey:
            raise JobParamError(
                f"override {key!r} not allowed (only clustering.* / crossref.* keys)"
            )
        if not subkey.replace("_", "").isalnum():
            raise JobParamError(f"override key {key!r} has an invalid name")
        # JSON-encode so numbers/bools/lists round-trip through the CLI's
        # json.loads-then-bare-string parsing (a quoted string decodes back
        # to a bare string).
        argv += ["--set", f"{key}={json.dumps(value)}"]
    return argv


def _build_report(params: dict) -> list[str]:
    argv = ["report"]
    run_id = _int(params, "run_id")
    if run_id is not None:
        argv += ["--run-id", str(run_id)]
    return argv


def _build_regen_crops(params: dict) -> list[str]:
    argv = ["regen-crops"]
    space = _str(params, "space")
    if space:
        argv += ["--space", space]
    # only_missing defaults to True on the CLI; emit --all to rebuild everything.
    if _get(params, "only_missing") is not None and not _bool(params, "only_missing", True):
        argv.append("--all")
    limit = _int(params, "limit")
    if limit is not None:
        argv += ["--limit", str(limit)]
    return argv


def _build_benchmark(params: dict) -> list[str]:
    argv = ["benchmark"]
    limit = _int(params, "limit")
    if limit is not None:
        argv += ["--limit", str(limit)]
    space = _str(params, "space")
    if space:
        argv += ["--space", space]
    warmup = _int(params, "warmup")
    if warmup is not None:
        argv += ["--warmup", str(warmup)]
    return argv


def _build_models_download(params: dict) -> list[str]:
    argv = ["models", "download"]
    only = _str_list(params, "only")
    if only:
        argv += ["--only", *only]
    if _bool(params, "allow_record_hash"):
        argv.append("--allow-record-hash")
    return argv


def _build_apply(params: dict) -> list[str]:
    """Base (safe) apply argv. Consent flags are added by validate_consent."""
    argv = ["apply"]
    kinds = _apply_kinds(params)
    if kinds:
        argv += ["--kinds", ",".join(kinds)]
    person_id = _int(params, "person_id")
    if person_id is not None:
        argv += ["--person-id", str(person_id)]
    space = _str(params, "space")
    if space:
        argv += ["--space", space]
    return argv


def _build_dedupe(params: dict) -> list[str]:
    """Base (dry-run) dedupe argv. --apply/-y are added by validate_consent."""
    exact = _bool(params, "exact")
    visual = _bool(params, "visual")
    if not exact and not visual:
        raise JobParamError("dedupe requires 'exact' and/or 'visual'")
    argv = ["dedupe"]
    if exact:
        argv.append("--exact")
    if visual:
        argv.append("--visual")
    threshold = _int(params, "threshold")
    if threshold is not None:
        if not 0 <= threshold <= 64:
            raise JobParamError("'threshold' must be between 0 and 64")
        argv += ["--threshold", str(threshold)]
    space = _str(params, "space")
    if space:
        argv += ["--space", space]
    return argv


def _build_reset(params: dict) -> list[str]:
    """Base reset argv. -y is added by validate_consent."""
    argv = ["reset"]
    if _bool(params, "all"):
        argv.append("--all")
    if _bool(params, "keep_crops"):
        argv.append("--keep-crops")
    return argv


def _build_clear_queue(params: dict) -> list[str]:
    return ["clear-queue"]


def _build_delete_crops(params: dict) -> list[str]:
    return ["delete-crops"]


#: The allowlist. `eval` stays CLI-only by design (not present here).
JOB_SPECS: dict[str, JobSpec] = {
    "sync": JobSpec("sync", _build_sync, DangerLevel.SAFE),
    "extract": JobSpec("extract", _build_extract, DangerLevel.SAFE),
    "cluster": JobSpec("cluster", _build_cluster, DangerLevel.SAFE),
    "recluster": JobSpec("recluster", _build_recluster, DangerLevel.SAFE),
    "report": JobSpec("report", _build_report, DangerLevel.SAFE),
    "regen-crops": JobSpec("regen-crops", _build_regen_crops, DangerLevel.SAFE),
    "benchmark": JobSpec("benchmark", _build_benchmark, DangerLevel.SAFE),
    "models-download": JobSpec("models-download", _build_models_download, DangerLevel.SAFE),
    "apply": JobSpec("apply", _build_apply, DangerLevel.TYPED_PHRASE),
    "dedupe": JobSpec("dedupe", _build_dedupe, DangerLevel.TYPED_PHRASE),
    "reset": JobSpec("reset", _build_reset, DangerLevel.TYPED_PHRASE),
    "clear-queue": JobSpec("clear-queue", _build_clear_queue, DangerLevel.CONFIRM),
    "delete-crops": JobSpec("delete-crops", _build_delete_crops, DangerLevel.CONFIRM),
}


# --------------------------------------------------------------------------- #
# Consent                                                                       #
# --------------------------------------------------------------------------- #

# Expected phrases live only here; ConsentError never leaks them.
_PHRASE_MERGE_NAMED = "merge named people"
_PHRASE_DEDUPE = "delete duplicates"
_PHRASE_RESET_ALL = "reset all"

# Flags that must never be produced by any code path in this module.
_FORBIDDEN_TOKENS = frozenset({"apply-all", "-Y"})


def _consent_apply(params: dict, confirm: bool, phrase: str | None) -> list[str]:
    # A dry-run preview runs without --apply: free, no consent required.
    if _bool(params, "dry_run", default=True):
        return []
    if not confirm:
        raise ConsentError("confirm", "confirm")
    extra = ["--apply"]
    kinds = set(_apply_kinds(params))
    if "reassign" in kinds:
        if not _bool(params, "apply_reassigns"):
            raise ConsentError("flag", "apply_reassigns", detail="reassign")
        extra.append("--apply-reassigns")
    if "merge" in kinds:
        if not _bool(params, "apply_merges"):
            raise ConsentError("flag", "apply_merges", detail="merge")
        extra.append("--apply-merges")
    if "merge_named" in kinds:
        if phrase != _PHRASE_MERGE_NAMED:
            raise ConsentError("phrase", "confirm_phrase", detail="merge_named")
        extra.append("--apply-merges-named")
    return extra


def _consent_dedupe(params: dict, confirm: bool, phrase: str | None) -> list[str]:
    if not _bool(params, "apply"):
        return []  # dry-run
    if phrase != _PHRASE_DEDUPE:
        raise ConsentError("phrase", "confirm_phrase", detail="dedupe")
    return ["--apply", "-y"]


def _consent_reset(params: dict, confirm: bool, phrase: str | None) -> list[str]:
    if _bool(params, "all"):
        if phrase != _PHRASE_RESET_ALL:
            raise ConsentError("phrase", "confirm_phrase", detail="reset_all")
    elif not confirm:
        raise ConsentError("confirm", "confirm")
    return ["-y"]


def validate_consent(
    spec: JobSpec, params: dict, confirm: bool, confirm_phrase: str | None
) -> list[str]:
    """Return the consent-gated flags to append to ``spec.build_argv(params)``.

    Raises ConsentError (→ HTTP 428) when the required confirmation, gate
    boolean, or typed phrase is absent. ``-y`` is appended only after the
    relevant consent validates; ``apply-all``/``-Y`` are never produced.
    """
    params = params or {}
    name = spec.name
    if name == "apply":
        extra = _consent_apply(params, confirm, confirm_phrase)
    elif name == "dedupe":
        extra = _consent_dedupe(params, confirm, confirm_phrase)
    elif name == "reset":
        extra = _consent_reset(params, confirm, confirm_phrase)
    elif name in ("clear-queue", "delete-crops"):
        if not confirm:
            raise ConsentError("confirm", "confirm")
        extra = ["-y"]
    else:
        extra = []  # SAFE commands need no consent
    return extra


def resolve_argv(
    name: str,
    params: dict | None = None,
    *,
    confirm: bool = False,
    confirm_phrase: str | None = None,
) -> list[str]:
    """Build the full, consent-validated CLI argv for a job name.

    Raises JobParamError for an unknown job or bad params, ConsentError when
    consent is missing. This is the single seam both the manager and tests use
    to exercise the argv/consent matrix.
    """
    spec = JOB_SPECS.get(name)
    if spec is None:
        raise JobParamError(f"unknown job: {name!r}")
    params = params or {}
    argv = list(spec.build_argv(params)) + validate_consent(
        spec, params, confirm, confirm_phrase
    )
    forbidden = _FORBIDDEN_TOKENS.intersection(argv)
    if forbidden:
        # Defensive: the GUI must never invoke apply-all or -Y.
        raise RuntimeError(f"consent bug: forbidden token(s) in argv: {sorted(forbidden)}")
    return argv


# --------------------------------------------------------------------------- #
# Job manager                                                                   #
# --------------------------------------------------------------------------- #

_MAX_IN_FLIGHT = 5
# Console output shares the ring with structured events, and a dry-run `dedupe`
# over a large library legitimately prints thousands of lines.
_RING_SIZE = 20000
_TAIL_INTERVAL = 0.25
_TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled", "interrupted"})
#: Replayed-from-disk event lists held in memory (a full job log each).
_REPLAY_CACHE_MAX = 8

#: Thread-count variables honoured by the numeric stacks a job pulls in. Left
#: alone, OpenBLAS/OpenMP size their pools to the machine and *busy-spin*
#: between calls, so a clustering job's `X @ X.T` puts one hot thread on every
#: core. The web server is a single process sharing those cores: its worker
#: threads then need tens of seconds of wall-clock to do a millisecond of work,
#: which reads from the outside as "the whole API froze" (the event loop, which
#: only ever wakes to do a few microseconds at a time, sails through — so the
#: watchdog reports no lag and an idle threadpool while requests take 90 s).
_THREAD_ENV_VARS = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
)
#: Default niceness for a job subprocess. Batch work by definition, and the one
#: thing it must not do is make the GUI that launched it unusable.
_JOB_NICE = 10


@dataclass
class Job:
    id: str
    name: str
    params: dict
    argv: list[str]
    dir: Path
    state: str = "queued"
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    ended_at: float | None = None
    pid: int | None = None
    exit_code: int | None = None
    error: str | None = None
    # runtime-only (not persisted)
    proc: subprocess.Popen | None = field(default=None, repr=False)
    cancel_requested: bool = False
    seq: int = 0
    events: deque = field(default_factory=lambda: deque(maxlen=_RING_SIZE), repr=False)
    latest: dict[str, dict] = field(default_factory=dict, repr=False)
    #: Most recent `progress` event, for the compact snapshot on job listings.
    last_progress: dict | None = field(default=None, repr=False)
    _tail_pos: int = 0
    # Independent read cursors for stdout.log / stderr.log, which are followed
    # alongside events.jsonl and surfaced as `log` events.
    _out_pos: dict[str, int] = field(default_factory=dict, repr=False)
    #: Last few stderr lines, kept to explain a non-zero exit that emitted no
    #: structured `error` event (an uncaught traceback, an import failure, ...).
    stderr_tail: deque = field(default_factory=lambda: deque(maxlen=20), repr=False)

    def meta(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "params": self.params,
            "argv": self.argv,
            "state": self.state,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "pid": self.pid,
            "exit_code": self.exit_code,
            "error": self.error,
        }


def _default_command(argv: list[str]) -> list[str]:
    return [sys.executable, "-m", "synopticon", *argv]


#: CSI escape sequences (colour, cursor moves) emitted by typer.secho / tqdm.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def _console_line(raw: str) -> str:
    """Normalize one console line for display, or '' if there is nothing to show.

    Two terminal-only behaviours have to be undone. Progress writers redraw in
    place with ``\\r``, so a single "line" can hold dozens of superseded frames —
    only the last one is current. And colour is expressed as ANSI escapes, which
    would render as literal noise in the browser.
    """
    if "\r" in raw:
        frames = [seg for seg in raw.split("\r") if seg.strip()]
        raw = frames[-1] if frames else ""
    return _ANSI_RE.sub("", raw).rstrip()


def _progress_snapshot(job: "Job") -> dict | None:
    """Compact `{phase, space, done, total, pct}` for a job listing, or None.

    Deliberately not part of ``Job.meta()``: that shape is what gets written to
    job.json, and a persisted progress figure would be stale the moment the job
    ends or the server restarts.
    """
    evt = job.last_progress
    if evt is None:
        return None
    done, total = evt.get("done"), evt.get("total")
    pct = None
    if isinstance(done, int) and isinstance(total, int) and total > 0:
        pct = min(100, max(0, round(done * 100 / total)))
    return {
        "phase": evt.get("phase"),
        "space": evt.get("space"),
        "done": done,
        "total": total,
        "pct": pct,
    }


def _failure_reason(job: "Job") -> str:
    """One-line explanation for a non-zero exit, from the tail of stderr.

    Prefers the last line of a Python traceback (the exception itself) over the
    frame listing above it; falls back to the exit code when stderr is empty.
    """
    lines = [ln for ln in job.stderr_tail if ln.strip()]
    for line in reversed(lines):
        # Skip traceback scaffolding (indented frames + source echo, and the
        # "Traceback" banner) to land on the message that explains it.
        if line.startswith((" ", "\t")):
            continue
        stripped = line.strip()
        if stripped.startswith(("Traceback (", "^", "~")):
            continue
        return stripped[:500]
    if lines:
        return lines[-1].strip()[:500]
    return f"exited with code {job.exit_code} and no output"


class JobManager:
    """Serialized subprocess job runner backed by flat files under ``jobs_dir``."""

    def __init__(
        self,
        jobs_dir: Path | str,
        *,
        specs: dict[str, JobSpec] | None = None,
        command_builder: Callable[[list[str]], list[str]] | None = None,
        thread_cap: int | None = None,
        nice: int = _JOB_NICE,
    ):
        self.jobs_dir = Path(jobs_dir)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._specs = specs if specs is not None else JOB_SPECS
        self._command_builder = command_builder or _default_command
        # `None` -> reserve one core for the server; 0 -> leave the environment
        # untouched (the operator is managing thread counts themselves).
        if thread_cap is None:
            thread_cap = max(1, (os.cpu_count() or 2) - 1)
        self._thread_cap = max(0, int(thread_cap))
        self._nice = max(0, int(nice))

        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        self._jobs: dict[str, Job] = {}
        #: job_id -> (identity, events) for jobs replayed off disk. Keyed on the
        #: job's terminal identity so a re-adopted job re-reads.
        self._replay_cache: dict[str, tuple[tuple, list[dict]]] = {}
        self._pending: deque[str] = deque()
        self._current: str | None = None
        self._stop = False
        self._next_id = self._scan_next_id()

        self._adopt_orphans()

        self._worker = threading.Thread(
            target=self._worker_loop, name="synopticon-jobs", daemon=True
        )
        self._worker.start()

    # -- submission --------------------------------------------------------- #

    def submit(
        self,
        name: str,
        params: dict | None = None,
        *,
        confirm: bool = False,
        confirm_phrase: str | None = None,
    ) -> str:
        """Validate + enqueue a job, returning its id.

        Raises JobParamError / ConsentError before anything is spawned, and
        QueueFullError when 5 jobs are already in flight.
        """
        if name not in self._specs:
            raise JobParamError(f"unknown job: {name!r}")
        params = dict(params or {})
        spec = self._specs[name]
        # Resolve argv against the *active* spec set (tests may inject specs).
        argv = list(spec.build_argv(params)) + validate_consent(
            spec, params, confirm, confirm_phrase
        )
        forbidden = _FORBIDDEN_TOKENS.intersection(argv)
        if forbidden:
            raise RuntimeError(f"consent bug: forbidden token(s) in argv: {sorted(forbidden)}")

        with self._cv:
            in_flight = sum(
                1 for j in self._jobs.values() if j.state in ("queued", "running")
            )
            if in_flight >= _MAX_IN_FLIGHT:
                raise QueueFullError(f"job queue is full ({_MAX_IN_FLIGHT} in flight)")
            job_id = str(self._next_id)
            self._next_id += 1
            job = Job(
                id=job_id,
                name=name,
                params=params,
                argv=argv,
                dir=self.jobs_dir / job_id,
            )
            job.dir.mkdir(parents=True, exist_ok=True)
            self._jobs[job_id] = job
            self._write_meta(job)
            self._pending.append(job_id)
            self._cv.notify_all()
        return job_id

    # -- queries ------------------------------------------------------------ #

    def get(self, job_id: str) -> dict | None:
        """Full metadata + live snapshot for one job, or None if unknown."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                data = self._with_progress(job)
                data["seq"] = job.seq
                data["latest"] = {p: dict(e) for p, e in job.latest.items()}
                return data
        meta = self._load_meta(self.jobs_dir / job_id)
        if meta is not None:
            # Match the in-memory shape so a replayed job is indistinguishable
            # to the API: `seq` is the highest replayed sequence number.
            replayed = self._replay(job_id)
            meta["seq"] = replayed[-1]["seq"] if replayed else 0
        return meta

    def events(self, job_id: str, after: int = 0) -> list[dict]:
        """Events for a job with ``seq > after`` (empty if unknown).

        Served from the in-memory ring for jobs this process ran, and replayed
        from the job directory for everything older. Without the replay every job
        that predates the current server process renders as an empty log — the
        history list happily shows it (that comes from ``job.json``) while the
        events, stdout and stderr sitting right next to it on disk are ignored.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                return [dict(e) for e in job.events if e.get("seq", 0) > after]
        return [e for e in self._replay(job_id) if e.get("seq", 0) > after]

    def _replay(self, job_id: str) -> list[dict]:
        """Reconstruct a finished job's event list from its directory (cached).

        Ordering is structured events, then stdout, then stderr: the console
        streams carry no timestamps, so a true interleave is not recoverable —
        and a live run's 250 ms poll batches them the same way. ``seq`` is
        assigned by position, which is stable because a terminal job's files no
        longer change, so the client's ``after=`` cursor keeps working.
        """
        job_dir = self.jobs_dir / job_id
        meta = self._load_meta(job_dir)
        if meta is None:
            return []
        key = (job_id, meta.get("state"), meta.get("ended_at"))
        with self._lock:
            cached = self._replay_cache.get(job_id)
            if cached is not None and cached[0] == key:
                return cached[1]

        events: list[dict] = []
        for line in self._read_lines(job_dir / "events.jsonl"):
            try:
                evt = json.loads(line)
            except (ValueError, json.JSONDecodeError):
                continue
            if isinstance(evt, dict):
                events.append(evt)
        for stream in ("stdout", "stderr"):
            level = "warning" if stream == "stderr" else "info"
            for line in self._read_lines(job_dir / f"{stream}.log"):
                text = _console_line(line)
                if text:
                    events.append(
                        {"v": 1, "event": "log", "level": level,
                         "message": text, "stream": stream}
                    )
        if meta.get("error"):
            events.append({"v": 1, "event": "error", "message": meta["error"]})
        if meta.get("state") in _TERMINAL_STATES:
            events.append(
                {"v": 1, "event": "final", "state": meta["state"],
                 "exit_code": meta.get("exit_code")}
            )
        for i, evt in enumerate(events, start=1):
            evt["seq"] = i

        # Only cache a finished job: a job still being written (one another
        # process is running) has no stable identity to key on, so caching it
        # would freeze its log at whatever it had reached.
        if meta.get("state") in _TERMINAL_STATES:
            with self._lock:
                if len(self._replay_cache) >= _REPLAY_CACHE_MAX:
                    self._replay_cache.clear()
                self._replay_cache[job_id] = (key, events)
        return events

    @staticmethod
    def _read_lines(path: Path) -> list[str]:
        """All lines of ``path``, newline translation off (empty if unreadable)."""
        try:
            with path.open("r", encoding="utf-8", errors="replace", newline="") as f:
                return f.read().split("\n")
        except OSError:
            return []

    def list_jobs(self) -> list[dict]:
        """In-memory jobs, newest first (created_at desc)."""
        with self._lock:
            metas = [self._with_progress(j) for j in self._jobs.values()]
        metas.sort(key=lambda m: m.get("created_at") or 0, reverse=True)
        return metas

    def history(self, limit: int = 50) -> list[dict]:
        """Newest ``limit`` jobs read from job.json on disk (survives restart).

        A job this process is running also carries its live ``progress``
        snapshot, so a listing (the topbar chip, the dashboard) can show how far
        along it is without opening a stream per job.
        """
        metas: list[dict] = []
        for d in self.jobs_dir.iterdir() if self.jobs_dir.is_dir() else []:
            meta = self._load_meta(d)
            if meta is None:
                continue
            with self._lock:
                job = self._jobs.get(meta.get("id") or d.name)
                if job is not None:
                    meta["progress"] = _progress_snapshot(job)
            metas.append(meta)
        metas.sort(key=lambda m: m.get("created_at") or 0, reverse=True)
        return metas[:limit]

    def _with_progress(self, job: Job) -> dict:
        """``job.meta()`` plus the live progress snapshot. Caller holds the lock."""
        meta = job.meta()
        meta["progress"] = _progress_snapshot(job)
        return meta

    # -- cancellation / shutdown ------------------------------------------- #

    def cancel(self, job_id: str) -> bool:
        """Cancel a queued or running job. Returns False if unknown/terminal."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.state in _TERMINAL_STATES:
                return False
            if job.state == "queued":
                try:
                    self._pending.remove(job_id)
                except ValueError:
                    pass
                job.cancel_requested = True
                self._set_state(job, "cancelled")
                return True
            # running
            job.cancel_requested = True
            proc = job.proc
        if proc is not None:
            self._escalate(proc)
        return True

    def shutdown(self, timeout: float = 20.0) -> None:
        """Stop the worker, cancelling any running job and dropping the queue."""
        with self._cv:
            self._stop = True
            while self._pending:
                jid = self._pending.popleft()
                j = self._jobs.get(jid)
                if j is not None and j.state == "queued":
                    j.cancel_requested = True
                    self._set_state(j, "cancelled")
            current = self._current
            self._cv.notify_all()
        if current:
            self.cancel(current)
        self._worker.join(timeout=timeout)

    # -- worker ------------------------------------------------------------- #

    def _worker_loop(self) -> None:
        while True:
            with self._cv:
                while not self._pending and not self._stop:
                    self._cv.wait()
                if self._stop:
                    return
                job_id = self._pending.popleft()
                job = self._jobs.get(job_id)
                if job is None or job.cancel_requested:
                    continue
                self._current = job_id
            try:
                self._run_job(job)
            except Exception as exc:  # never let the worker die
                with self._lock:
                    job.error = repr(exc)
                    job.ended_at = time.time()
                    self._set_state(job, "failed")
            finally:
                with self._cv:
                    self._current = None
                    self._cv.notify_all()

    def _run_job(self, job: Job) -> None:
        with self._lock:
            if job.cancel_requested:
                job.ended_at = time.time()
                self._set_state(job, "cancelled")
                return
            job.started_at = time.time()

        events_path = job.dir / "events.jsonl"
        env = os.environ.copy()
        env["SYNOPTICON_PROGRESS_FILE"] = str(events_path)
        env["PYTHONUNBUFFERED"] = "1"
        # Must be in the environment *before* exec: the numeric stacks read these
        # once, at import, and size their pools for the life of the process.
        for var in _THREAD_ENV_VARS:
            if self._thread_cap and var not in env:
                env[var] = str(self._thread_cap)
        command = self._command_builder(job.argv)

        stdout_f = (job.dir / "stdout.log").open("wb")
        stderr_f = (job.dir / "stderr.log").open("wb")
        tailer: threading.Thread | None = None
        try:
            proc = subprocess.Popen(
                command,
                cwd=str(Path.cwd()),
                env=env,
                stdout=stdout_f,
                stderr=stderr_f,
                start_new_session=True,
            )
        except OSError as exc:
            stdout_f.close()
            stderr_f.close()
            with self._lock:
                job.error = repr(exc)
                job.ended_at = time.time()
                self._set_state(job, "failed")
            return

        self._renice(proc.pid)

        # Publish proc *before* marking running so a cancel that raced the
        # queued->running transition can always find the process to signal;
        # if it already landed, kill the child immediately.
        with self._lock:
            job.proc = proc
            job.pid = proc.pid
            self._set_state(job, "running")
            escalate_now = job.cancel_requested

        tailer = threading.Thread(
            target=self._tail,
            args=(job, lambda: proc.poll() is None),
            name=f"tail-{job.id}",
            daemon=True,
        )
        tailer.start()
        if escalate_now:
            self._escalate(proc)

        rc = proc.wait()
        tailer.join(timeout=5.0)
        stdout_f.close()
        stderr_f.close()

        with self._lock:
            job.exit_code = rc
            job.ended_at = time.time()
            if job.cancel_requested:
                state = "cancelled"
            elif rc == 0:
                state = "succeeded"
            else:
                state = "failed"
            job.proc = None
            explained = any(e.get("event") == "error" for e in job.events)
            reason = _failure_reason(job) if state == "failed" and not explained else None
            if reason:
                job.error = reason
            self._set_state(job, state)
        # A failure the command did not narrate itself must still say *something*
        # useful: promote the tail of stderr into a real `error` event so the UI
        # has a headline instead of just a red "failed" chip.
        if reason:
            self._ingest_event(
                job,
                {
                    "v": 1,
                    "ts": time.time(),
                    "event": "error",
                    "message": reason,
                    "exit_code": rc,
                },
            )
        # Synthesize the authoritative terminal event (exit code is truth).
        self._ingest(
            job,
            json.dumps(
                {
                    "v": 1,
                    "ts": time.time(),
                    "event": "final",
                    "state": job.state,
                    "exit_code": rc,
                }
            ),
        )

    # -- event tailing ------------------------------------------------------ #

    def _tail(self, job: Job, is_running: Callable[[], bool]) -> None:
        """Follow events.jsonl *and* stdout/stderr into the ring until exit.

        The console streams matter as much as the structured events: a command
        that dies before it emits anything (bad config, missing model weights,
        an uncaught traceback) would otherwise leave the GUI with a completely
        empty log and no way to tell *why* it failed. Everything a terminal user
        would have seen is therefore mirrored as `log` events, tagged with the
        stream it came from.
        """
        path = job.dir / "events.jsonl"
        buf = ""
        console: dict[str, str] = {"stdout": "", "stderr": ""}
        while True:
            exited = not is_running()
            chunk = self._read_new(job, path, "events")
            if chunk:
                buf += chunk
                lines = buf.split("\n")
                buf = lines.pop()  # keep any partial trailing line
                for line in lines:
                    line = line.strip()
                    if line:
                        self._ingest(job, line)
            for stream in ("stdout", "stderr"):
                console[stream] = self._tail_console(job, stream, console[stream])
            if exited:
                break
            time.sleep(_TAIL_INTERVAL)
        # Flush trailing partial lines (no newline written before exit).
        tail = buf.strip()
        if tail:
            self._ingest(job, tail)
        for stream in ("stdout", "stderr"):
            self._tail_console(job, stream, console[stream], flush=True)

    def _read_new(self, job: Job, path: Path, key: str) -> str:
        """Read everything appended to ``path`` since this job's last read."""
        pos = job._tail_pos if key == "events" else job._out_pos.get(key, 0)
        try:
            if not path.exists():
                return ""
            # newline="" disables universal-newline translation. Without it a
            # progress writer's `\r` redraws arrive already rewritten to `\n`,
            # i.e. as hundreds of separate lines that can no longer be collapsed
            # back to the final frame.
            with path.open("r", encoding="utf-8", errors="replace", newline="") as f:
                f.seek(pos)
                chunk = f.read()
                pos = f.tell()
        except OSError:
            return ""
        if key == "events":
            job._tail_pos = pos
        else:
            job._out_pos[key] = pos
        return chunk

    def _tail_console(self, job: Job, stream: str, buf: str, flush: bool = False) -> str:
        """Ingest newly-written ``<stream>.log`` lines; return the partial remainder."""
        buf += self._read_new(job, job.dir / f"{stream}.log", stream)
        lines = buf.split("\n")
        # Hold back the trailing partial line until its newline arrives; on the
        # final flush there is nothing more coming, so take it as-is.
        buf = "" if flush else lines.pop()
        level = "warning" if stream == "stderr" else "info"
        for raw in lines:
            text = _console_line(raw)
            if not text:
                continue
            if stream == "stderr":
                job.stderr_tail.append(text)
            self._ingest_event(
                job,
                {
                    "v": 1,
                    "ts": time.time(),
                    "event": "log",
                    "level": level,
                    "message": text,
                    "stream": stream,
                },
            )
        return buf

    def _ingest(self, job: Job, line: str) -> None:
        try:
            evt = json.loads(line)
            if not isinstance(evt, dict):
                raise ValueError("event is not an object")
        except (ValueError, json.JSONDecodeError):
            evt = {
                "v": 1,
                "event": "log",
                "level": "error",
                "message": line,
                "malformed": True,
            }
        self._ingest_event(job, evt)

    def _ingest_event(self, job: Job, evt: dict) -> None:
        with self._lock:
            job.seq += 1
            evt["seq"] = job.seq
            job.events.append(evt)
            phase = evt.get("phase")
            if isinstance(phase, str) and phase:
                job.latest[phase] = evt
            if evt.get("event") == "progress":
                job.last_progress = evt

    # -- child resource limits ---------------------------------------------- #

    def _renice(self, pid: int) -> None:
        """Lower the child's scheduling priority. Best-effort, never fatal.

        Applied from the parent rather than via ``preexec_fn``: running Python
        between fork and exec is documented as unsafe in a threaded program
        (this process has a worker, a tailer and the whole AnyIO pool), and it
        also forces CPython off its ``posix_spawn``/vfork fast path.

        Doing it after ``Popen`` is a race on paper only. Linux niceness is
        per-thread and inherited at thread creation, so what matters is landing
        before the child spawns its BLAS/OpenMP pool — that happens on the first
        matmul, seconds to minutes into a job, while this runs microseconds
        after the fork.
        """
        if not self._nice or not hasattr(os, "setpriority"):
            return
        try:
            os.setpriority(os.PRIO_PROCESS, pid, self._nice)
        except OSError:
            pass

    # -- cancellation escalation ------------------------------------------- #

    def _escalate(self, proc: subprocess.Popen) -> None:
        """SIGINT → 10 s → SIGTERM → 5 s → SIGKILL over the child's group."""
        try:
            pgid = os.getpgid(proc.pid)
        except OSError:
            pgid = None

        def send(sig: int) -> None:
            try:
                if pgid is not None:
                    os.killpg(pgid, sig)
                else:
                    proc.send_signal(sig)
            except (OSError, ProcessLookupError):
                pass

        send(signal.SIGINT)
        if self._wait_dead(proc, 10.0):
            return
        send(signal.SIGTERM)
        if self._wait_dead(proc, 5.0):
            return
        send(signal.SIGKILL)

    @staticmethod
    def _wait_dead(proc: subprocess.Popen, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return True
            time.sleep(0.1)
        return proc.poll() is not None

    # -- persistence -------------------------------------------------------- #

    def _set_state(self, job: Job, state: str) -> None:
        """Set state and rewrite job.json. Caller holds the lock."""
        job.state = state
        self._write_meta(job)

    def _write_meta(self, job: Job) -> None:
        tmp = job.dir / "job.json.tmp"
        try:
            tmp.write_text(json.dumps(job.meta(), indent=2), encoding="utf-8")
            os.replace(tmp, job.dir / "job.json")
        except OSError:
            pass

    def _load_meta(self, job_dir: Path) -> dict | None:
        meta_path = job_dir / "job.json"
        try:
            return json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _scan_next_id(self) -> int:
        """Next sequential job id: one past the highest numeric dir on disk.

        Pre-sequential dirs (``<epoch-ms>-<hex>``) are skipped so legacy
        history stays readable without inflating the counter.
        """
        highest = 0
        if self.jobs_dir.is_dir():
            for d in self.jobs_dir.iterdir():
                if d.name.isascii() and d.name.isdigit():
                    highest = max(highest, int(d.name))
        return highest + 1

    # -- startup adoption --------------------------------------------------- #

    def _adopt_orphans(self) -> None:
        if not self.jobs_dir.is_dir():
            return
        for d in sorted(self.jobs_dir.iterdir()):
            meta = self._load_meta(d)
            if meta is None or meta.get("state") != "running":
                continue
            pid = meta.get("pid")
            job = Job(
                id=meta.get("id") or d.name,
                name=meta.get("name") or "?",
                params=meta.get("params") or {},
                argv=meta.get("argv") or [],
                dir=d,
                state="running",
                created_at=meta.get("created_at") or time.time(),
                started_at=meta.get("started_at"),
                pid=pid,
            )
            self._jobs[job.id] = job
            if isinstance(pid, int) and self._pid_alive(pid) and self._is_synopticon(pid):
                # Live orphan: re-tail and monitor until the pid dies.
                threading.Thread(
                    target=self._monitor_orphan,
                    args=(job, pid),
                    name=f"adopt-{job.id}",
                    daemon=True,
                ).start()
            else:
                job.ended_at = time.time()
                self._set_state(job, "interrupted")

    def _monitor_orphan(self, job: Job, pid: int) -> None:
        self._tail(job, lambda: self._pid_alive(pid))
        with self._lock:
            job.ended_at = time.time()
            self._set_state(job, self._terminal_from_events(job))

    @staticmethod
    def _terminal_from_events(job: Job) -> str:
        """Best-effort final state for an orphan (no exit code available)."""
        for evt in reversed(job.events):
            ev = evt.get("event")
            if ev == "result":
                return "succeeded" if evt.get("ok", True) else "failed"
            if ev == "error":
                return "failed"
        return "interrupted"

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # exists but not ours
        except OSError:
            return False
        return True

    @staticmethod
    def _is_synopticon(pid: int) -> bool:
        """Guard against pid reuse: the process must still be a synopticon run."""
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            return False
        return b"synopticon" in raw
