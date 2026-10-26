# ADR 21 — The lifetime run ledger behind the Stats page

**Status:** Accepted
**Applies to:** migration `0012_command_runs.sql`, `runlog.py`, `cli.py::_main`, `progress.Emitter.result`, `web/jobs.py` (job origin env), `web/scheduler.py`, `web/stats_page.py`, `web/stats_routes.py`, `db/copy.py::TABLES`

## Context

The Stats page has two halves: what the library *is* (photos, faces, people, how faces spread
across people) and what Synopticon has *done* over its lifetime (how often each command ran, how
many faces it ever found, how many corrections it wrote to the NAS).

The library half can be read straight off the current tables. The lifetime half cannot, because
almost nothing the database holds is lifetime:

- **CLI runs leave no trace.** The GUI's job directories (`jobs/<id>/job.json` + `events.jsonl`)
  record GUI and scheduled runs, but a `synopticon sync` from a terminal or a host cron writes
  nothing anywhere.
- **Current row counts are not totals.** A re-extract deletes and re-inserts a photo's faces
  (`faces.face_id` is AUTOINCREMENT, so `MAX(face_id)` overcounts and `COUNT(*)` undercounts), and
  `reset` empties faces, embeddings, clusters and the review queue.
- **`audit_log` is the exception.** Nothing clears it, so NAS writes are already lifetime.

## Decision

### The CLI records every run in `command_runs`

`cli.py::_main` (the Typer callback every command passes through) inserts a `running` row before
the command runs and finishes it when the command's context closes — `ok`, `failed` or
`cancelled`, plus the command's last `emitter.result(stats=…)` payload as `stats_json`.
`Emitter.result()` remembers that payload even when no progress file is set, so a terminal run
carries the same numbers a GUI job would.

Recording at the CLI rather than in the job manager is the point: GUI jobs are CLI subprocesses, so
one hook counts terminal, GUI and scheduled runs identically. The job manager only says *who*
started it, via `SYNOPTICON_JOB_ORIGIN` (`gui` / `schedule`) and `SYNOPTICON_JOB_ID` in the child
environment; unset means `cli`.

A row with `finished_at IS NULL` is a run that never closed — still going, or killed outright
(OOM, `SIGKILL`). It is shown as "didn't finish", not guessed at.

### The ledger never gets in a command's way

Every ledger step is wrapped and logged at debug level. A missing config, an unreachable database
or a failed insert leaves the command's behaviour, output and exit code untouched — a statistic is
never worth a failed sync.

Not recorded: `--help` and bare invocations, `web` (a server never "finishes", and counting its
restarts says nothing about the library), and `db-migrate` (it moves the database the row would be
written to).

### Lifetime means lifetime

`command_runs` is never pruned, and `reset` / `reset --all` leave it alone, like `audit_log`. It is
in `db/copy.py::TABLES` so `db-migrate` carries it, and in the backup snapshot (it holds no
credential).

### History before the ledger is imported once, from the job directories

On web start-up, off the event loop, the job directories are imported once — GUI and scheduled runs
with their result payloads — and `sync_state['command_runs.backfilled']` marks it done. Rows are
deduplicated on `job_id`, since a job started after this change records itself — and since
`reset --all` clears `sync_state`, marker included, so the import may run again. Terminal runs from
before the ledger existed are unrecoverable; the page says "tracking since" its first row.

### The library half is computed on request and cached by fingerprint

It is O(library), so it sits behind a one-entry, fingerprint-keyed cache in the web process
(the `LookupCache` pattern, ADR 07) and is rebuilt in the threadpool only when the counted tables
change. The lifetime half reads small tables and is computed per request. The page refetches both
when a job finishes, by watching the existing jobs poller rather than adding a timer.

### Face distribution uses Synology's labels

"Faces per person" counts `syno_faces` per listed, named person. Mapping our own detections onto
people would be a whole-library IoU pass for a figure that, after `apply`, says the same thing.
Unlisted and unnamed persons (ADR 19, ADR 20) are never shown by name; their faces are one bucket.

## Consequences

- A command killed before its context closes keeps a `running` row forever; the page reports these
  as unfinished rather than trying to tell "still running" from "died".
- Lifetime figures summed from `stats_json` depend on each command's result shape. The extractors
  ignore what they do not recognise, so changing a result payload silently drops it from the
  totals rather than breaking the page — check `web/stats_page.py` when renaming a result key.
- "Faces found" counts re-detections: a `pipeline_version` bump that re-extracts the library finds
  every face again, and that is work Synopticon did.
