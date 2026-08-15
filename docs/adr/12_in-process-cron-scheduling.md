# ADR 12 — In-process cron scheduling

**Status:** Accepted
**Applies to:** `cron.py`, `web/schedules.py`, `web/scheduler.py`, `web/schedule_routes.py`, migration `0008`

## Context

Users want `sync` to run nightly. In a container whose only long-running process is
`synopticon web`, there is no cron daemon to hang that off, and asking people to run a second
container or a host crontab defeats the point of a self-contained image.

The jobs being scheduled are long — an `extract` can run for 30 hours — and the machine may
suspend, change clocks, or be down when an occurrence was due.

## Decision

A cron parser and one daemon thread inside the web process.

### `cron.py` is a dependency-free leaf

Like `cpu.py` and `progress.py` (ADR 01). A 5-field Vixie parser plus `next_fire`, zone-aware via
`zoneinfo`.

Two DST choices, both biased toward *late rather than never*, because these are batch jobs:

- a nonexistent wall time (spring forward) fires at the shifted instant
- an ambiguous wall time (fall back) fires once, at `fold=0`

### The scheduler is a daemon thread, not an event-loop task

A tick opens SQLite and can block on the job-manager lock, so running it on the loop would violate
ADR 07's invariant (1).

It ticks every 20 s and compares stored epoch timestamps rather than counting intervals, so
suspend/resume and clock changes self-correct instead of accumulating drift.

### Two deliberate behaviours

**No backfill.** An occurrence missed while the server was down is recorded `missed`, not queued.
Waking to four stacked syncs after a restart is worse than missing one. `_CATCHUP_GRACE` still runs
a firing missed by seconds.

**No overlap per command.** If a job of the same name is already queued or running, the firing is
recorded `skipped` — so a 30-hour `extract` never stacks on itself.

### The catalog is served with the listing

`schedules.py` owns `SCHEDULABLE`, the *presentation* half of each job's form. The parameter
whitelist stays in `jobs.py` (ADR 05), and the catalog is served alongside the listing so the SPA
form cannot drift from it.

`schedules.py` also owns validation and CRUD over the `schedules` and `schedule_runs` tables.

`schedule_runs` is a bounded diagnostic tail — 20 per schedule. Real writes still land in
`audit_log` via the job itself.

### "Run now" reuses the timer's path

It goes through the identical `Scheduler.fire` path with `manual=True`, so it can never do
something the timer could not, and it leaves `next_run_at` alone.

## Consequences

- Schedules replay a stored *submission*, never a stored argv, and `confirm_phrase` is always
  `None`. Every typed-phrase job is unschedulable by construction. See ADR 05 for the full
  argument.
- Adding a schedulable parameter is a Python-side change only; the frontend renders whatever the
  catalog describes.
- `schedule_routes.py` must not carry `from __future__ import annotations` (ADR 08).
