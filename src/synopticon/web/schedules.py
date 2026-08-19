"""Saved job schedules: the catalog of what may be scheduled, plus CRUD.

A schedule is a *stored submission* — a job name, its whitelisted params, the
answer to its confirmation gate, and a cron expression — that the scheduler
thread (:mod:`synopticon.web.scheduler`) replays on a timer. Everything here is
DB-only and framework-free; the routes in ``schedule_routes.py`` and the thread
are thin layers over it.

Safety model (this is the load-bearing part — see CLAUDE.md's "Safety model"):

* **A schedule may never carry a typed phrase.** ``validate`` and the scheduler
  both call the job layer with ``confirm_phrase=None``, so any form that needs
  one — ``merge_named``, ``dedupe --apply``, ``reset --all`` — raises
  ``ConsentError`` and cannot be saved *or* fired. The phrase tier exists to
  prove a human typed it deliberately at that moment, and a cron entry is by
  definition nobody typing anything. That is structural, not a UI convention.
* The schedulable set is a strict subset of ``JOB_SPECS`` (:data:`SCHEDULABLE`)
  rather than "everything that validates": ``reset`` is confirm-tier and would
  otherwise pass, and a nightly wipe of local pipeline state has no legitimate
  periodic use.
* Every stored schedule is re-validated through ``jobs.resolve_argv`` at save
  time *and* re-submitted through ``JobManager.submit`` at fire time, so it can
  never become a path around a gate that is tightened later.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone as dt_timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..db import Connection, Row

from .. import cron
from .jobs import ConsentError, JobParamError, resolve_argv

#: Firing history kept per schedule; older rows are pruned on every write.
_RUNS_KEPT = 20

#: Statuses a `schedule_runs` row can carry.
RUN_SUBMITTED = "submitted"
RUN_SKIPPED = "skipped"
RUN_MISSED = "missed"
RUN_ERROR = "error"


class ScheduleError(ValueError):
    """A schedule was invalid. The web layer maps this to HTTP 422."""


# --------------------------------------------------------------------------- #
# Catalog                                                                       #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FormField:
    """One editable parameter, described for the SPA's generic form renderer.

    The param *whitelist* lives in ``jobs.py``'s ``build_argv``; this is only the
    presentation half. Keeping it server-side means the schedule form cannot
    drift from the jobs that actually exist.
    """

    key: str
    label: str
    type: str  # "bool" | "int" | "text" | "select" | "multiselect"
    options: tuple[str, ...] = ()
    help: str = ""
    default: Any = None


@dataclass(frozen=True)
class ScheduleForm:
    job: str
    label: str
    description: str
    fields: tuple[FormField, ...] = ()
    #: True when the job's non-dry-run form needs the plain confirmation gate.
    needs_confirm: bool = False
    warning: str = ""


_SPACE = FormField(
    "space",
    "Space",
    "select",
    options=("", "personal", "shared"),
    help="Leave empty for every configured space.",
)

#: What the GUI may put on a timer. A strict subset of ``JOB_SPECS`` — see the
#: module docstring for why ``reset`` is absent and why nothing here can reach
#: the typed-phrase tier.
SCHEDULABLE: dict[str, ScheduleForm] = {
    "sync": ScheduleForm(
        "sync",
        "Sync from NAS",
        "Pull new photos, albums and Synology person labels. Read-only toward the NAS.",
        fields=(
            _SPACE,
            FormField("hash", "Compute content hashes", "bool", help="--hash"),
            FormField("skip_faces", "Skip Synology faces", "bool"),
            FormField("all_faces", "Re-fetch all faces", "bool"),
        ),
    ),
    "extract": ScheduleForm(
        "extract",
        "Detect faces",
        "Scan photos that have not been processed yet and record every face found.",
        fields=(
            _SPACE,
            FormField("limit", "Photo limit", "int", help="0 or empty for no limit."),
        ),
    ),
    "cluster": ScheduleForm(
        "cluster",
        "Group faces",
        "Group detected faces by person, match them against your Synology people and "
        "generate review-queue items.",
    ),
    "recluster": ScheduleForm(
        "recluster",
        "Re-group faces (offline)",
        "Group the faces again from what is already stored, using the saved config. "
        "Never touches the NAS.",
    ),
    "regen-crops": ScheduleForm(
        "regen-crops",
        "Regenerate crops",
        "Rebuild face crop images used by the review UI.",
        fields=(
            _SPACE,
            FormField(
                "only_missing",
                "Only missing crops",
                "bool",
                default=True,
                help="Off rebuilds every crop (--all).",
            ),
            FormField("limit", "Limit", "int"),
        ),
    ),
    "report": ScheduleForm(
        "report", "Static HTML report", "Regenerate the standalone review report."
    ),
    "apply": ScheduleForm(
        "apply",
        "Apply approved corrections",
        "Write approved review decisions back to the NAS. Dry-run unless you turn it off.",
        fields=(
            FormField(
                "kinds",
                "Kinds",
                "multiselect",
                options=("assign", "low_confidence", "new_person", "reassign", "merge"),
                help="Empty means the CLI default: assign + low_confidence.",
            ),
            FormField(
                "dry_run",
                "Dry run only",
                "bool",
                default=True,
                help="Preview without writing anything.",
            ),
            FormField("apply_reassigns", "Allow reassigns", "bool"),
            FormField("apply_merges", "Allow merges", "bool"),
            _SPACE,
        ),
        needs_confirm=True,
        warning=(
            "A scheduled apply writes to the NAS unattended. Named↔named merges "
            "can never be scheduled — they need a typed confirmation every time."
        ),
    ),
    "dedupe": ScheduleForm(
        "dedupe",
        "Duplicate report (dry run)",
        "List duplicate photos from the stored hashes. Deleting them cannot be "
        "scheduled — that needs a typed confirmation every time.",
        fields=(
            FormField("exact", "Byte-identical (sha256)", "bool", default=True),
            FormField("visual", "Near-duplicates (pHash)", "bool"),
            FormField("threshold", "pHash distance", "int", help="0-64, default 6."),
            _SPACE,
        ),
    ),
    "clear-queue": ScheduleForm(
        "clear-queue",
        "Clear review queue",
        "Drop pending review items. Approved and applied decisions are kept.",
        needs_confirm=True,
    ),
    "clear-applies": ScheduleForm(
        "clear-applies",
        "Clear queued applies",
        "Send approved-but-unwritten and failed decisions back to pending for "
        "another look. Nothing is deleted and the NAS is untouched.",
        needs_confirm=True,
    ),
    "delete-crops": ScheduleForm(
        "delete-crops",
        "Delete crop images",
        "Wipe cached face crops to reclaim disk.",
        needs_confirm=True,
    ),
}


def catalog() -> list[dict]:
    """The schedulable job catalog, JSON-ready for the SPA."""
    return [asdict(form) for form in SCHEDULABLE.values()]


# --------------------------------------------------------------------------- #
# Validation                                                                    #
# --------------------------------------------------------------------------- #


def resolve_tz(name: str | None):
    """A ``ZoneInfo`` for ``name``, or the system local zone when empty."""
    if not name:
        return datetime.now().astimezone().tzinfo or dt_timezone.utc
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        raise ScheduleError(f"unknown timezone {name!r}") from None


@dataclass
class ValidatedSchedule:
    name: str
    job: str
    params: dict
    confirm: bool
    cron: str
    timezone: str | None
    enabled: bool
    expr: cron.CronExpr = field(repr=False, default=None)  # type: ignore[assignment]


def validate(body: dict) -> ValidatedSchedule:
    """Validate a create/update payload, raising :class:`ScheduleError`.

    Runs the *real* job argv/consent resolution with ``confirm_phrase=None``, so
    a form that needs a typed phrase is rejected here rather than at 3 a.m.
    """
    if not isinstance(body, dict):
        raise ScheduleError("body must be an object")

    job = (body.get("job") or "").strip()
    if job not in SCHEDULABLE:
        raise ScheduleError(
            f"job {job!r} cannot be scheduled"
            if job
            else "a job name is required"
        )

    name = (body.get("name") or "").strip() or SCHEDULABLE[job].label
    if len(name) > 120:
        raise ScheduleError("name is too long (max 120 characters)")

    params = body.get("params") or {}
    if not isinstance(params, dict):
        raise ScheduleError("'params' must be an object")

    expression = body.get("cron")
    try:
        expr = cron.parse(expression if isinstance(expression, str) else "")
    except cron.CronError as exc:
        raise ScheduleError(str(exc)) from None

    tz_name = (body.get("timezone") or "").strip() or None
    resolve_tz(tz_name)  # raises ScheduleError on a bad zone

    confirm = bool(body.get("confirm", False))
    try:
        resolve_argv(job, params, confirm=confirm, confirm_phrase=None)
    except ConsentError as exc:
        if exc.requirement == "phrase":
            raise ScheduleError(
                f"this form of '{job}' requires a typed confirmation and cannot be "
                "scheduled — run it by hand"
            ) from None
        raise ScheduleError(
            f"'{job}' needs confirmation for this configuration "
            f"(missing: {exc.field})"
        ) from None
    except JobParamError as exc:
        raise ScheduleError(str(exc)) from None

    return ValidatedSchedule(
        name=name,
        job=job,
        params=params,
        confirm=confirm,
        cron=expr.source,
        timezone=tz_name,
        enabled=bool(body.get("enabled", True)),
        expr=expr,
    )


def compute_next(
    expr: cron.CronExpr, tz_name: str | None, after: float | None = None
) -> int:
    """Epoch seconds of the next firing after ``after`` (default: now)."""
    moment = datetime.fromtimestamp(
        after if after is not None else time.time(), dt_timezone.utc
    )
    return int(cron.next_fire(expr, moment, resolve_tz(tz_name)).timestamp())


def preview(expression: str, tz_name: str | None, count: int = 5) -> list[int]:
    """Next ``count`` firings for an expression, as epoch seconds."""
    try:
        expr = cron.parse(expression)
    except cron.CronError as exc:
        raise ScheduleError(str(exc)) from None
    now = datetime.now(dt_timezone.utc)
    try:
        fires = cron.next_fires(expr, now, resolve_tz(tz_name), count)
    except cron.CronError as exc:
        raise ScheduleError(str(exc)) from None
    return [int(dt.timestamp()) for dt in fires]


# --------------------------------------------------------------------------- #
# CRUD                                                                          #
# --------------------------------------------------------------------------- #


def _row_to_dict(row: Row) -> dict:
    data = dict(row)
    try:
        data["params"] = json.loads(data.pop("params_json") or "{}")
    except (ValueError, TypeError):
        data["params"] = {}
    data["confirm"] = bool(data.get("confirm"))
    data["enabled"] = bool(data.get("enabled"))
    form = SCHEDULABLE.get(data.get("job") or "")
    data["job_label"] = form.label if form else data.get("job")
    return data


def list_schedules(conn: Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM schedules ORDER BY enabled DESC, next_run_at IS NULL, "
        "next_run_at ASC, id ASC"
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_schedule(conn: Connection, schedule_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM schedules WHERE id = ?", (int(schedule_id),)
    ).fetchone()
    return _row_to_dict(row) if row else None


def create(conn: Connection, valid: ValidatedSchedule) -> dict:
    now = int(time.time())
    next_run = compute_next(valid.expr, valid.timezone) if valid.enabled else None
    cur = conn.execute(
        "INSERT INTO schedules (name, job, params_json, confirm, cron, timezone, "
        "enabled, created_at, updated_at, next_run_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            valid.name,
            valid.job,
            json.dumps(valid.params),
            int(valid.confirm),
            valid.cron,
            valid.timezone,
            int(valid.enabled),
            now,
            now,
            next_run,
        ),
    )
    conn.commit()
    created = get_schedule(conn, int(cur.lastrowid))
    assert created is not None
    return created


def update(conn: Connection, schedule_id: int, valid: ValidatedSchedule) -> dict | None:
    if get_schedule(conn, schedule_id) is None:
        return None
    next_run = compute_next(valid.expr, valid.timezone) if valid.enabled else None
    conn.execute(
        "UPDATE schedules SET name = ?, job = ?, params_json = ?, confirm = ?, "
        "cron = ?, timezone = ?, enabled = ?, updated_at = ?, next_run_at = ? "
        "WHERE id = ?",
        (
            valid.name,
            valid.job,
            json.dumps(valid.params),
            int(valid.confirm),
            valid.cron,
            valid.timezone,
            int(valid.enabled),
            int(time.time()),
            next_run,
            int(schedule_id),
        ),
    )
    conn.commit()
    return get_schedule(conn, schedule_id)


def set_enabled(conn: Connection, schedule_id: int, enabled: bool) -> dict | None:
    row = get_schedule(conn, schedule_id)
    if row is None:
        return None
    next_run = None
    if enabled:
        try:
            next_run = compute_next(cron.parse(row["cron"]), row["timezone"])
        except (cron.CronError, ScheduleError):
            next_run = None
    conn.execute(
        "UPDATE schedules SET enabled = ?, updated_at = ?, next_run_at = ? WHERE id = ?",
        (int(bool(enabled)), int(time.time()), next_run, int(schedule_id)),
    )
    conn.commit()
    return get_schedule(conn, schedule_id)


def delete(conn: Connection, schedule_id: int) -> bool:
    cur = conn.execute("DELETE FROM schedules WHERE id = ?", (int(schedule_id),))
    conn.execute(
        "DELETE FROM schedule_runs WHERE schedule_id = ?", (int(schedule_id),)
    )
    conn.commit()
    return cur.rowcount > 0


# --------------------------------------------------------------------------- #
# Firing bookkeeping                                                            #
# --------------------------------------------------------------------------- #


def record_run(
    conn: Connection,
    schedule_id: int,
    status: str,
    *,
    job_id: str | None = None,
    detail: str | None = None,
    fired_at: float | None = None,
    next_run_at: int | None = None,
) -> None:
    """Append a firing record and roll the schedule's summary columns forward."""
    stamp = int(fired_at if fired_at is not None else time.time())
    conn.execute(
        "INSERT INTO schedule_runs (schedule_id, fired_at, job_id, status, detail) "
        "VALUES (?, ?, ?, ?, ?)",
        (int(schedule_id), stamp, job_id, status, detail),
    )
    conn.execute(
        "UPDATE schedules SET last_run_at = ?, last_job_id = ?, last_status = ?, "
        "next_run_at = ? WHERE id = ?",
        (stamp, job_id, status, next_run_at, int(schedule_id)),
    )
    # Keep the history bounded: this table is a diagnostic tail, not an audit log
    # (every actual write still lands in `audit_log` via the job itself).
    conn.execute(
        "DELETE FROM schedule_runs WHERE schedule_id = ? AND id NOT IN ("
        "  SELECT id FROM schedule_runs WHERE schedule_id = ? "
        "  ORDER BY fired_at DESC, id DESC LIMIT ?)",
        (int(schedule_id), int(schedule_id), _RUNS_KEPT),
    )
    conn.commit()


def runs(conn: Connection, schedule_id: int, limit: int = _RUNS_KEPT) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM schedule_runs WHERE schedule_id = ? "
        "ORDER BY fired_at DESC, id DESC LIMIT ?",
        (int(schedule_id), int(limit)),
    ).fetchall()
    return [dict(r) for r in rows]


def due(conn: Connection, now: float) -> list[dict]:
    """Enabled schedules whose next firing is at or before ``now``."""
    rows = conn.execute(
        "SELECT * FROM schedules WHERE enabled = 1 AND next_run_at IS NOT NULL "
        "AND next_run_at <= ? ORDER BY next_run_at ASC",
        (int(now),),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def set_next_run(conn: Connection, schedule_id: int, next_run_at: int | None) -> None:
    conn.execute(
        "UPDATE schedules SET next_run_at = ? WHERE id = ?",
        (next_run_at, int(schedule_id)),
    )
    conn.commit()
