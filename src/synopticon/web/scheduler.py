"""The cron thread that fires saved schedules into the job manager.

One daemon thread owned by the web app's lifespan. Every tick it asks the DB for
schedules whose ``next_run_at`` has arrived and submits them through the normal
:meth:`JobManager.submit` path — the same allowlist, the same param whitelist,
the same consent validation a human's click goes through. A schedule is a saved
submission, never a stored argv.

Why a thread rather than the event loop: a tick opens a SQLite connection and
may block on the job manager's lock, and both are exactly the kind of work the
web app's responsiveness invariants keep off the loop. It is also why the loop
never touches FastAPI, and why the manual "run now" route hands in its *own*
connection instead of borrowing this thread's.

Deliberate behaviours:

* **No backfill.** An occurrence that fell while the server was down is recorded
  as ``missed`` and skipped. These jobs are hours-long batch work; waking up to
  four queued syncs because the container restarted is worse than missing one.
  A firing that was only just missed (``_CATCHUP_GRACE``) still runs, so a
  restart does not silently drop a schedule that was seconds away.
* **No overlap per command.** If the same job name is already queued or running
  the firing is recorded as ``skipped``. A nightly ``extract`` that takes 30
  hours must not stack.
* The tick interval is coarse (20 s) and every decision is made by comparing
  stored epoch timestamps to the current time, so suspend/resume and clock
  changes resolve themselves on the next pass instead of accumulating drift.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from ..db import Connection

from . import schedules
from .jobs import ConsentError, JobError, JobParamError, QueueFullError

log = logging.getLogger(__name__)

#: How often the loop wakes to look for due schedules.
_TICK = 20.0
#: A firing this far in the past still runs on the next tick; older ones are
#: recorded as missed. Covers a server restart, not a night off.
_CATCHUP_GRACE = 300.0


class Scheduler:
    """Fires due schedules into ``job_manager``. Start/stop from the lifespan."""

    def __init__(
        self,
        conn_factory: Callable[[], Connection],
        job_manager,
        *,
        tick: float = _TICK,
    ) -> None:
        self._conn = conn_factory
        self._jm = job_manager
        self._tick = float(tick)
        self._wake = threading.Event()
        self._stop = False
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------- #

    def start(self) -> None:
        if self._thread is not None:
            return
        self._bootstrap()
        self._thread = threading.Thread(
            target=self._loop, name="synopticon-scheduler", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop = True
        self._wake.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout)

    def wake(self) -> None:
        """Ask the loop to tick now (after a create/update/enable)."""
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop:
            self._wake.wait(self._tick)
            self._wake.clear()
            if self._stop:
                return
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - the thread must never die
                log.exception("scheduler tick failed")

    # -- startup reconciliation --------------------------------------------- #

    def _bootstrap(self) -> None:
        """Give every enabled schedule a sane ``next_run_at`` for *this* boot.

        A schedule with no next firing (freshly enabled, or written by an older
        version) gets one; one whose firing fell while the process was down is
        recorded as missed and rolled forward.
        """
        now = time.time()
        try:
            c = self._conn()
        except Exception:  # noqa: BLE001
            log.exception("scheduler could not open the database")
            return
        try:
            for row in schedules.list_schedules(c):
                if not row["enabled"]:
                    continue
                nxt = row["next_run_at"]
                if nxt is not None and nxt >= now - _CATCHUP_GRACE:
                    continue
                following = self._reschedule(row, now)
                if nxt is None:
                    schedules.set_next_run(c, row["id"], following)
                    continue
                log.warning(
                    "schedule %s (%s) missed its %s firing while the server was down",
                    row["id"],
                    row["name"],
                    time.strftime("%Y-%m-%d %H:%M", time.localtime(nxt)),
                )
                schedules.record_run(
                    c,
                    row["id"],
                    schedules.RUN_MISSED,
                    detail="server was not running at the scheduled time",
                    fired_at=nxt,
                    next_run_at=following,
                )
        finally:
            c.close()

    # -- firing -------------------------------------------------------------- #

    def tick(self, now: float | None = None) -> int:
        """Fire every due schedule once. Returns how many were submitted."""
        moment = time.time() if now is None else now
        c = self._conn()
        try:
            due = schedules.due(c, moment)
            submitted = 0
            for row in due:
                if self.fire(c, row, now=moment) is not None:
                    submitted += 1
            return submitted
        finally:
            c.close()

    def fire(
        self,
        conn: Connection,
        row: dict,
        *,
        now: float | None = None,
        manual: bool = False,
    ) -> str | None:
        """Submit one schedule's job. Returns the job id, or None if not started.

        ``manual`` marks an operator-triggered "run now": it still goes through
        the identical submit path, but it does not consume or move the schedule's
        ``next_run_at``.
        """
        moment = time.time() if now is None else now
        following = None if manual else self._reschedule(row, moment)

        def record(status: str, *, job_id: str | None = None, detail: str | None = None):
            if manual:
                # A manual run is history, not a firing: keep the row so the UI
                # can show it, but leave next_run_at exactly where it was.
                schedules.record_run(
                    conn,
                    row["id"],
                    status,
                    job_id=job_id,
                    detail=detail,
                    fired_at=moment,
                    next_run_at=row.get("next_run_at"),
                )
            else:
                schedules.record_run(
                    conn,
                    row["id"],
                    status,
                    job_id=job_id,
                    detail=detail,
                    fired_at=moment,
                    next_run_at=following,
                )

        if self._already_running(row["job"]):
            log.info(
                "schedule %s (%s): '%s' is already in flight, skipping this firing",
                row["id"],
                row["name"],
                row["job"],
            )
            record(schedules.RUN_SKIPPED, detail=f"'{row['job']}' was already in flight")
            return None

        try:
            # confirm_phrase is *always* None here. Every typed-phrase form is
            # therefore unreachable from a schedule, whatever is stored in the
            # row — see schedules.py's module docstring.
            job_id = self._jm.submit(
                row["job"],
                dict(row["params"]),
                confirm=bool(row["confirm"]),
                confirm_phrase=None,
            )
        except QueueFullError as exc:
            log.warning("schedule %s (%s): %s", row["id"], row["name"], exc)
            record(schedules.RUN_SKIPPED, detail=str(exc))
            return None
        except (ConsentError, JobParamError, JobError) as exc:
            log.error(
                "schedule %s (%s) is no longer valid: %s", row["id"], row["name"], exc
            )
            record(schedules.RUN_ERROR, detail=str(exc))
            return None

        log.info(
            "schedule %s (%s) started job %s (%s)",
            row["id"],
            row["name"],
            job_id,
            row["job"],
        )
        record(schedules.RUN_SUBMITTED, job_id=job_id)
        return job_id

    # -- helpers ------------------------------------------------------------- #

    def _already_running(self, job_name: str) -> bool:
        try:
            return any(
                j.get("name") == job_name and j.get("state") in ("queued", "running")
                for j in self._jm.list_jobs()
            )
        except Exception:  # noqa: BLE001 - never block a firing on a listing bug
            return False

    def _reschedule(self, row: dict, after: float) -> int | None:
        """Next firing strictly after ``after``, or None if it cannot be computed."""
        from .. import cron

        try:
            expr = cron.parse(row["cron"])
            return schedules.compute_next(expr, row.get("timezone"), after=after)
        except (cron.CronError, schedules.ScheduleError) as exc:
            log.error(
                "schedule %s (%s) has an unusable cron expression %r: %s",
                row["id"],
                row["name"],
                row.get("cron"),
                exc,
            )
            return None
