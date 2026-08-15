# ADR 06 — Jobs run as resource-constrained subprocesses with a file-based progress protocol

**Status:** Accepted
**Applies to:** `progress.py`, `web/jobs.py`, `cli.py`, `cpu.py`

## Context

The web GUI runs the same long operations the CLI does — an `extract` over a large library takes
tens of hours. Three constraints shaped the design:

1. The server is a single uvicorn process. Running a job in-process would block it entirely
   (ADR 07).
2. A job that produces no visible output for hours is indistinguishable from a hung one.
3. A job and the server share one machine. An unconstrained job starves the server that launched
   it.

## Decision

Jobs are subprocesses. They communicate progress through a file, their bookkeeping is flat files,
and their CPU appetite is capped by the parent.

### Progress protocol v1 (`progress.py`)

A dependency-free leaf module, so `cluster/` may import it without breaching the ADR 01 boundary.

- `SYNOPTICON_PROGRESS_FILE=<path>` enables a JSONL emitter. Unset gives a cached no-op and
  byte-identical terminal output.
- Event kinds: `phase`, `progress` (throttled to ≥100 ms, but always emits `done == total`),
  `log`, `result`, `error`.
- It never raises — write errors are swallowed. Instrumentation must not be able to kill a
  multi-hour run.
- **The process exit code is authoritative.** `JobManager` synthesizes the terminal `final` event
  from it; `result` is advisory.

Two call-site rules:

- **Keep `progress()`'s phase name identical to the `phase()` that opened it.** `cli.py::_progress`
  takes a `phase=` override for exactly this. A mismatch renders one phase as two chips.
- **`progress` on a job listing (`_progress_snapshot`) is never part of `Job.meta()`.** `meta()` is
  what gets written to `job.json`, and a persisted percentage outlives the run that produced it.

Redundant console echoes are suppressed rather than mirrored twice: `cli.py::_progress` skips its
plain-text line and `pipeline/runner.py` passes `disable=emitter.enabled` to tqdm, since the
structured `progress` event already drives the bar. Terminal behaviour is unchanged when the
environment variable is unset.

### Bookkeeping is flat files, with no pipeline-database coupling

`data/jobs/<id>/` holds `events.jsonl`, `stdout.log`, `stderr.log`, and `job.json` (rewritten on
state change).

One worker thread drains a FIFO of at most 5. A 250 ms tailer follows `events.jsonl` into a
`seq`-cursored ring buffer. On startup, `running` jobs are re-adopted only if their pid is still a
live `synopticon` process (guarded by reading `/proc/<pid>/cmdline`); otherwise they are marked
`interrupted`.

### Job output must never be empty or stale — four mechanisms, all load-bearing

1. **Heartbeat.** `Emitter.progress` narrates itself into the `log` stream every 30 s with rate and
   ETA, and once at phase end with elapsed time (suppressed under 1 s — `"0 done in 0s"` is worse
   than silence). `phase()` resets the per-phase baseline, because the same phase name recurs per
   space and carrying timings over reports a nonsense rate. Instrumenting a loop with `progress()`
   therefore gets scrollback narration for free; don't hand-roll it per call site.

2. **Log bridge.** `progress.install_log_bridge()`, called once from `cli.py`'s `@app.callback()`,
   attaches an `EventLogHandler` to the whole `synopticon` logger tree, so every module's existing
   `log.info`/`log.warning` becomes a `log` event with no call-site changes. That is the intended
   way to add commentary. `syno/writeback.py`'s `synopticon.apply` logger sets `propagate = False`,
   which is what stops it double-emitting.

3. **Console mirroring.** The tailer follows `stdout.log`/`stderr.log` too, as `log` events tagged
   `stream` (stdout → info, stderr → warning), ANSI-stripped, with `\r` redraws collapsed to their
   final frame. So `typer.echo` output needs no separate instrumentation, and a command that dies
   *before* emitting any event still explains itself. **Read those files with `newline=""`** —
   universal-newline translation rewrites `\r` to `\n` and the collapse silently stops working.
   A non-zero exit that emitted no `error` event gets one synthesized from the tail of stderr
   (skipping traceback scaffolding to land on the exception line) into both the event stream and
   `job.error`.

4. **Disk replay.** `JobManager.events()` falls back to `_replay()` for any job not in `_jobs`.
   Without it, every job predating the current server process rendered an empty log while the
   history list happily showed it. Replay order is events, then stdout, then stderr — the console
   streams carry no timestamps, so the live interleave is not recoverable. `seq` is positional and
   therefore stable, so the client's `after=` cursor and SSE resume keep working. Only *terminal*
   jobs are cached, since a job another process is still writing has no stable identity to key on,
   and `/api/jobs/{id}/stream` warms the cache via `run_in_threadpool` before entering its async
   generator — otherwise the replay's file reads land on the event loop (ADR 07, invariant 1).

### Job subprocesses are resource-constrained

`_run_job` sets `_THREAD_ENV_VARS` — `OMP_NUM_THREADS` plus the OpenBLAS/MKL/numexpr/veclib
aliases — to `JobManager(thread_cap=…)`. The default is `available_cores() - 1`, the app passes
`settings.inference.job_threads`, `0` leaves the environment alone, and an already-exported value
always wins. It then `_renice`s the child to `settings.inference.job_nice` (default 10).

Left unconstrained, `cluster/graph.py`'s `X @ X.T` hands off to OpenBLAS, which sizes its pool to
the machine and *busy-spins*. The server's worker threads then take 30–90 s of wall-clock to do a
millisecond of work, while the event loop — which only ever wakes for microseconds — sails
through. The watchdog reports no lag and an idle threadpool, and requests time out anyway.

`_renice` runs in the parent after `Popen` rather than via `preexec_fn`: Python between fork and
exec is unsafe in a threaded process and forfeits the `posix_spawn` fast path. Linux niceness is
per-thread and inherited at thread creation, and the child's BLAS pool is not built until its
first matmul, so the microsecond race is theoretical.

**Core counts come from `synopticon/cpu.py`, never `os.cpu_count()` or `/proc/cpuinfo` directly.**
Neither is namespaced, so inside a container both enumerate the *host's* CPUs and a 2-core
container would size a 31-thread BLAS pool.

- `available_cores()` = affinity mask ∩ cgroup CPU quota (v2 `cpu.max`, v1
  `cfs_quota_us`/`cfs_period_us`).
- `physical_cores()` — the `intra_op_threads` default, re-exported by `pipeline/onnx_session.py`
  for its old callers — is the `/proc/cpuinfo` physical count capped by that.
- `hwinfo` prints the quota as a "Usable cores" row.

## Consequences

- A new long-running command gets progress narration by calling `phase()` and `progress()`; it
  does not need its own logging strategy.
- Anything that reads job files on the request path must go through `run_in_threadpool`.
