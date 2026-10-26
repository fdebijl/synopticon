"""The run ledger: one `command_runs` row per CLI invocation.

Every function here is meant to be called inside a try/except by its caller —
the ledger must never change what a command does.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from synopticon.db import Connection, store

log = logging.getLogger(__name__)

ENV_ORIGIN = "SYNOPTICON_JOB_ORIGIN"
ENV_JOB_ID = "SYNOPTICON_JOB_ID"
BACKFILL_KEY = "command_runs.backfilled"

_STATUS_FROM_JOB_STATE = {
    "succeeded": "ok",
    "ok": "ok",
    "failed": "failed",
    "cancelled": "cancelled",
}

#: Job names whose CLI root command is spelled differently.
_COMMAND_FROM_JOB_NAME = {"models-download": "models"}


def origin_from_env() -> tuple[str, str | None]:
    origin = os.environ.get(ENV_ORIGIN, "")
    return (origin if origin in ("gui", "schedule") else "cli"), os.environ.get(ENV_JOB_ID) or None


def start(
    conn: Connection,
    command: str,
    origin: str,
    job_id: str | None,
    started_at: int | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO command_runs (command, origin, job_id, started_at, status) "
        "VALUES (?, ?, ?, ?, 'running')",
        (command, origin, job_id, store.now() if started_at is None else started_at),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish(conn: Connection, run_id: int, status: str, stats: Any | None) -> None:
    conn.execute(
        "UPDATE command_runs SET finished_at = ?, status = ?, stats_json = ? WHERE id = ?",
        (
            store.now(),
            status,
            json.dumps(stats, default=str) if stats is not None else None,
            run_id,
        ),
    )
    conn.commit()


def _last_result_stats(events: Path) -> Any | None:
    stats = None
    try:
        with events.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict) and obj.get("event") == "result":
                    stats = obj.get("stats")
    except OSError:
        return None
    return stats


def backfill_from_jobs(conn: Connection, jobs_dir: Path) -> int:
    """Import past GUI jobs into the ledger once; returns how many rows were added."""
    if store.get_state(conn, BACKFILL_KEY):
        return 0
    known = {r[0] for r in conn.execute(
        "SELECT job_id FROM command_runs WHERE job_id IS NOT NULL"
    ).fetchall()}
    added = 0
    dirs = sorted(jobs_dir.iterdir()) if jobs_dir.is_dir() else []
    for d in dirs:
        try:
            meta = json.loads((d / "job.json").read_text(encoding="utf-8"))
            if not isinstance(meta, dict):
                continue
            job_id = str(meta.get("id") or d.name)
            if job_id in known:
                continue
            argv = meta.get("argv")
            command = argv[0] if isinstance(argv, list) and argv else _COMMAND_FROM_JOB_NAME.get(
                meta.get("name"), meta.get("name")
            )
            started = meta.get("started_at") or meta.get("created_at")
            if not command or started is None:
                continue
            status = _STATUS_FROM_JOB_STATE.get(meta.get("state"))
            ended = meta.get("ended_at")
            stats = _last_result_stats(d / "events.jsonl")
            conn.execute(
                "INSERT INTO command_runs "
                "(command, origin, job_id, started_at, finished_at, status, stats_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(command),
                    meta.get("origin") or "gui",
                    job_id,
                    int(started),
                    int(ended) if status and ended is not None else None,
                    status or "running",
                    json.dumps(stats, default=str) if stats is not None else None,
                ),
            )
            conn.commit()
            known.add(job_id)
            added += 1
        except Exception:
            conn.rollback()
            log.debug("run ledger backfill skipped %s", d, exc_info=True)
    store.set_state(conn, BACKFILL_KEY, True)
    return added
