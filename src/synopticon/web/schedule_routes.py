"""``/api/schedules/*``: CRUD for saved job schedules, plus the cron preview.

Registered onto the app by :func:`register_schedule_routes`. The data layer is
:mod:`synopticon.web.schedules` and the timer is
:class:`synopticon.web.scheduler.Scheduler` — this module is only the HTTP shape.

Every mutation is validated by ``schedules.validate``, which re-resolves the job
argv with ``confirm_phrase=None``; a form needing a typed confirmation is
therefore rejected with 422 at save time rather than failing at 3 a.m. (and the
scheduler re-validates again when it fires).

Like every other write path in the GUI the mutating handlers are ``async def``
(they await a JSON body) and push their SQLite work through
``run_in_threadpool`` — see the responsiveness invariants in ``app.py``.

This module must **not** carry ``from __future__ import annotations``: FastAPI
resolves handler annotations against module globals, and ``Request`` is imported
inside the registrar. With postponed evaluation every route silently degrades its
``Request`` parameter to a required query field.
"""

import sqlite3
from typing import Callable

from ..config import Settings
from . import schedules


def register_schedule_routes(
    app,
    settings: Settings,
    conn: Callable[[], sqlite3.Connection],
    job_manager,
    scheduler,
) -> None:
    """Attach the schedules API to ``app``."""
    from fastapi import Request
    from fastapi.responses import JSONResponse
    from starlette.concurrency import run_in_threadpool

    def _error(exc: schedules.ScheduleError):
        return JSONResponse({"error": str(exc)}, status_code=422)

    def _job_states() -> dict[str, str]:
        """``job id -> state`` for the recent job history, resolved once per request.

        Deliberately *not* ``job_manager.get(id)`` per run: for a job this process
        did not run, ``get`` replays ``events.jsonl`` + the full stdout/stderr off
        disk just to compute a sequence number — a dry-run ``dedupe`` log is
        thousands of lines, and this endpoint would pay that per firing shown.
        ``history`` only reads each ``job.json``.
        """
        try:
            return {
                j["id"]: j.get("state")
                for j in job_manager.history(limit=500)
                if j.get("id")
            }
        except Exception:  # noqa: BLE001 - the state chip is advisory
            return {}

    def _runs_with_state(
        c: sqlite3.Connection, schedule_id: int, states: dict[str, str]
    ) -> list[dict]:
        out = []
        for run in schedules.runs(c, schedule_id, limit=10):
            job_id = run.get("job_id")
            if job_id:
                run["job_state"] = states.get(str(job_id))
            out.append(run)
        return out

    def _decorate(rows: list[dict], c: sqlite3.Connection) -> list[dict]:
        """Attach each schedule's recent firings and their jobs' states."""
        states = _job_states()
        for row in rows:
            row["runs"] = _runs_with_state(c, row["id"], states)
        return rows

    @app.get("/api/schedules")
    def api_list_schedules():
        c = conn()
        try:
            return {
                "items": _decorate(schedules.list_schedules(c), c),
                "jobs": schedules.catalog(),
            }
        finally:
            c.close()

    @app.get("/api/schedules/{schedule_id}")
    def api_get_schedule(schedule_id: int):
        c = conn()
        try:
            row = schedules.get_schedule(c, schedule_id)
            if row is None:
                return JSONResponse({"error": "unknown schedule"}, status_code=404)
            row["runs"] = _runs_with_state(c, schedule_id, _job_states())
            return row
        finally:
            c.close()

    @app.post("/api/schedules/preview")
    async def api_preview(request: Request):
        """Next few firings for an expression — the form's live validation."""
        body = await request.json()
        try:
            fires = schedules.preview(
                body.get("cron") or "", (body.get("timezone") or "").strip() or None
            )
        except schedules.ScheduleError as exc:
            return _error(exc)
        return {"next": fires}

    @app.post("/api/schedules")
    async def api_create_schedule(request: Request):
        body = await request.json()
        try:
            valid = schedules.validate(body)
        except schedules.ScheduleError as exc:
            return _error(exc)

        def work():
            c = conn()
            try:
                return schedules.create(c, valid)
            finally:
                c.close()

        row = await run_in_threadpool(work)
        scheduler.wake()
        return JSONResponse(row, status_code=201)

    @app.put("/api/schedules/{schedule_id}")
    async def api_update_schedule(request: Request, schedule_id: int):
        body = await request.json()
        try:
            valid = schedules.validate(body)
        except schedules.ScheduleError as exc:
            return _error(exc)

        def work():
            c = conn()
            try:
                return schedules.update(c, schedule_id, valid)
            finally:
                c.close()

        row = await run_in_threadpool(work)
        if row is None:
            return JSONResponse({"error": "unknown schedule"}, status_code=404)
        scheduler.wake()
        return row

    @app.post("/api/schedules/{schedule_id}/enabled")
    async def api_set_enabled(request: Request, schedule_id: int):
        body = await request.json()
        enabled = bool(body.get("enabled", True))

        def work():
            c = conn()
            try:
                return schedules.set_enabled(c, schedule_id, enabled)
            finally:
                c.close()

        row = await run_in_threadpool(work)
        if row is None:
            return JSONResponse({"error": "unknown schedule"}, status_code=404)
        scheduler.wake()
        return row

    @app.delete("/api/schedules/{schedule_id}")
    async def api_delete_schedule(schedule_id: int):
        def work():
            c = conn()
            try:
                return schedules.delete(c, schedule_id)
            finally:
                c.close()

        if not await run_in_threadpool(work):
            return JSONResponse({"error": "unknown schedule"}, status_code=404)
        return {"ok": True}

    @app.post("/api/schedules/{schedule_id}/run")
    async def api_run_now(schedule_id: int):
        """Fire a schedule immediately without disturbing its next firing.

        Goes through the identical ``Scheduler.fire`` path — same submit, same
        consent validation, same "already in flight" guard — so "run now" can
        never do something the timer could not.
        """

        def work():
            c = conn()
            try:
                row = schedules.get_schedule(c, schedule_id)
                if row is None:
                    return None
                job_id = scheduler.fire(c, row, manual=True)
                return {"job_id": job_id, "schedule": schedules.get_schedule(c, schedule_id)}
            finally:
                c.close()

        result = await run_in_threadpool(work)
        if result is None:
            return JSONResponse({"error": "unknown schedule"}, status_code=404)
        if result["job_id"] is None:
            return JSONResponse(
                {
                    "error": "not started",
                    "detail": (result["schedule"] or {}).get("last_status"),
                },
                status_code=409,
            )
        return JSONResponse(result, status_code=202)
