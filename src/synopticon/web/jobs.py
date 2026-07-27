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
* On startup, jobs left `running` by a crashed server are re-adopted if their
  pid is still a live `synopticon` process, else marked `interrupted`.
"""

from __future__ import annotations

import enum
import json
import os
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
_RING_SIZE = 5000
_TAIL_INTERVAL = 0.25
_TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled", "interrupted"})


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
    _tail_pos: int = 0

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


class JobManager:
    """Serialized subprocess job runner backed by flat files under ``jobs_dir``."""

    def __init__(
        self,
        jobs_dir: Path | str,
        *,
        specs: dict[str, JobSpec] | None = None,
        command_builder: Callable[[list[str]], list[str]] | None = None,
    ):
        self.jobs_dir = Path(jobs_dir)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._specs = specs if specs is not None else JOB_SPECS
        self._command_builder = command_builder or _default_command

        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        self._jobs: dict[str, Job] = {}
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
                data = job.meta()
                data["seq"] = job.seq
                data["latest"] = {p: dict(e) for p, e in job.latest.items()}
                return data
        return self._load_meta(self.jobs_dir / job_id)

    def events(self, job_id: str, after: int = 0) -> list[dict]:
        """Ring-buffered events for a job with ``seq > after`` (empty if unknown)."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return []
            return [dict(e) for e in job.events if e.get("seq", 0) > after]

    def list_jobs(self) -> list[dict]:
        """In-memory jobs, newest first (created_at desc)."""
        with self._lock:
            metas = [j.meta() for j in self._jobs.values()]
        metas.sort(key=lambda m: m.get("created_at") or 0, reverse=True)
        return metas

    def history(self, limit: int = 50) -> list[dict]:
        """Newest ``limit`` jobs read from job.json on disk (survives restart)."""
        metas: list[dict] = []
        for d in self.jobs_dir.iterdir() if self.jobs_dir.is_dir() else []:
            meta = self._load_meta(d)
            if meta is not None:
                metas.append(meta)
        metas.sort(key=lambda m: m.get("created_at") or 0, reverse=True)
        return metas[:limit]

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
            self._set_state(job, state)
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
        """Follow events.jsonl into the ring buffer until the process exits."""
        path = job.dir / "events.jsonl"
        buf = ""
        while True:
            exited = not is_running()
            chunk = ""
            try:
                if path.exists():
                    with path.open("r", encoding="utf-8", errors="replace") as f:
                        f.seek(job._tail_pos)
                        chunk = f.read()
                        job._tail_pos = f.tell()
            except OSError:
                chunk = ""
            if chunk:
                buf += chunk
                lines = buf.split("\n")
                buf = lines.pop()  # keep any partial trailing line
                for line in lines:
                    line = line.strip()
                    if line:
                        self._ingest(job, line)
            if exited:
                break
            time.sleep(_TAIL_INTERVAL)
        # Flush a trailing line with no newline.
        tail = buf.strip()
        if tail:
            self._ingest(job, tail)

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
        with self._lock:
            job.seq += 1
            evt["seq"] = job.seq
            job.events.append(evt)
            phase = evt.get("phase")
            if isinstance(phase, str) and phase:
                job.latest[phase] = evt

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
