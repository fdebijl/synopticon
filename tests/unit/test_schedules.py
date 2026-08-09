"""Saved job schedules: catalog/consent validation, CRUD, and the cron thread.

No subprocesses and no FastAPI here — the scheduler is exercised against a fake
job manager and a real SQLite file, one deterministic ``tick()`` at a time.
"""

from __future__ import annotations

import time

import pytest

from synopticon.config import load_settings
from synopticon.db import store
from synopticon.web import schedules
from synopticon.web.jobs import QueueFullError
from synopticon.web.scheduler import Scheduler


@pytest.fixture
def settings(tmp_path):
    return load_settings(
        storage={"data_dir": tmp_path},
        nas={"url": "https://nas.test", "account": "svc", "password": "pw"},
    )


@pytest.fixture
def db(settings):
    c = store.connect(settings.storage.db_path)
    yield c
    c.close()


@pytest.fixture
def conn_factory(settings):
    return lambda: store.connect(settings.storage.db_path)


class FakeJobManager:
    """Records submissions; mimics JobManager's in-flight listing."""

    def __init__(self):
        self.submitted: list[tuple] = []
        self.jobs: list[dict] = []
        self.raise_with: Exception | None = None
        self._next = 1

    def submit(self, name, params=None, *, confirm=False, confirm_phrase=None):
        if self.raise_with is not None:
            raise self.raise_with
        self.submitted.append((name, params, confirm, confirm_phrase))
        job_id = str(self._next)
        self._next += 1
        self.jobs.append({"id": job_id, "name": name, "state": "running"})
        return job_id

    def list_jobs(self):
        return list(self.jobs)

    def get(self, job_id):
        for j in self.jobs:
            if j["id"] == job_id:
                return j
        return None


def _payload(**over):
    body = {"name": "Nightly sync", "job": "sync", "cron": "0 3 * * *", "params": {}}
    body.update(over)
    return body


# --------------------------------------------------------------------------- #
# Validation / consent
# --------------------------------------------------------------------------- #
def test_validate_accepts_a_safe_job():
    valid = schedules.validate(_payload())
    assert valid.job == "sync"
    assert valid.cron == "0 3 * * *"
    assert valid.enabled is True


def test_unknown_or_unschedulable_jobs_are_refused():
    for job in ("nonsense", "reset", "apply-all", "eval"):
        with pytest.raises(schedules.ScheduleError):
            schedules.validate(_payload(job=job))


def test_bad_cron_is_refused():
    with pytest.raises(schedules.ScheduleError) as exc:
        schedules.validate(_payload(cron="0 99 * * *"))
    assert "hour" in str(exc.value)


def test_bad_timezone_is_refused():
    with pytest.raises(schedules.ScheduleError):
        schedules.validate(_payload(timezone="Mars/Olympus"))


def test_apply_dry_run_needs_no_confirmation():
    valid = schedules.validate(
        _payload(job="apply", params={"dry_run": True, "kinds": ["assign"]})
    )
    assert valid.confirm is False


def test_apply_write_without_confirm_is_refused():
    with pytest.raises(schedules.ScheduleError):
        schedules.validate(
            _payload(job="apply", params={"dry_run": False, "kinds": ["assign"]})
        )


def test_apply_write_with_confirm_is_accepted():
    valid = schedules.validate(
        _payload(
            job="apply",
            params={"dry_run": False, "kinds": ["assign", "low_confidence"]},
            confirm=True,
        )
    )
    assert valid.confirm is True


def test_merge_needs_its_gate_boolean():
    with pytest.raises(schedules.ScheduleError):
        schedules.validate(
            _payload(
                job="apply", params={"dry_run": False, "kinds": ["merge"]}, confirm=True
            )
        )
    valid = schedules.validate(
        _payload(
            job="apply",
            params={"dry_run": False, "kinds": ["merge"], "apply_merges": True},
            confirm=True,
        )
    )
    assert valid.params["apply_merges"] is True


def test_typed_phrase_forms_can_never_be_scheduled():
    """merge_named / dedupe --apply need a phrase, so they must not be storable."""
    with pytest.raises(schedules.ScheduleError) as exc:
        schedules.validate(
            _payload(
                job="apply",
                params={"dry_run": False, "kinds": ["merge_named"]},
                confirm=True,
            )
        )
    assert "typed confirmation" in str(exc.value)

    with pytest.raises(schedules.ScheduleError) as exc:
        schedules.validate(
            _payload(job="dedupe", params={"exact": True, "apply": True}, confirm=True)
        )
    assert "typed confirmation" in str(exc.value)


def test_dedupe_dry_run_is_schedulable():
    assert schedules.validate(_payload(job="dedupe", params={"exact": True})).job == "dedupe"


def test_catalog_is_a_subset_of_the_job_allowlist():
    from synopticon.web.jobs import JOB_SPECS

    assert set(schedules.SCHEDULABLE) <= set(JOB_SPECS)
    assert "reset" not in schedules.SCHEDULABLE
    entries = schedules.catalog()
    assert {e["job"] for e in entries} == set(schedules.SCHEDULABLE)
    assert all(e["label"] for e in entries)


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #
def test_create_list_update_delete(db):
    row = schedules.create(db, schedules.validate(_payload()))
    assert row["id"] > 0
    assert row["next_run_at"] > time.time()
    assert row["params"] == {}
    assert row["job_label"]

    listed = schedules.list_schedules(db)
    assert [r["id"] for r in listed] == [row["id"]]

    updated = schedules.update(
        db, row["id"], schedules.validate(_payload(name="Later", cron="0 5 * * *"))
    )
    assert updated["name"] == "Later"
    assert updated["cron"] == "0 5 * * *"

    assert schedules.update(db, 999, schedules.validate(_payload())) is None
    assert schedules.delete(db, row["id"]) is True
    assert schedules.delete(db, row["id"]) is False
    assert schedules.list_schedules(db) == []


def test_disabling_clears_the_next_run(db):
    row = schedules.create(db, schedules.validate(_payload()))
    off = schedules.set_enabled(db, row["id"], False)
    assert off["enabled"] is False and off["next_run_at"] is None
    on = schedules.set_enabled(db, row["id"], True)
    assert on["enabled"] is True and on["next_run_at"] > time.time()


def test_disabled_schedule_is_never_due(db):
    schedules.create(db, schedules.validate(_payload(cron="* * * * *", enabled=False)))
    assert schedules.due(db, time.time() + 86400) == []


def test_run_history_is_pruned(db):
    row = schedules.create(db, schedules.validate(_payload()))
    for i in range(30):
        schedules.record_run(
            db, row["id"], schedules.RUN_SUBMITTED, job_id=str(i), fired_at=1000 + i
        )
    history = schedules.runs(db, row["id"], limit=100)
    assert len(history) == 20
    assert history[0]["job_id"] == "29"
    assert schedules.get_schedule(db, row["id"])["last_job_id"] == "29"


def test_deleting_a_schedule_drops_its_runs(db):
    row = schedules.create(db, schedules.validate(_payload()))
    schedules.record_run(db, row["id"], schedules.RUN_SUBMITTED, job_id="1")
    schedules.delete(db, row["id"])
    assert schedules.runs(db, row["id"]) == []


def test_preview_returns_increasing_timestamps():
    fires = schedules.preview("*/10 * * * *", None, count=4)
    assert len(fires) == 4 and fires == sorted(fires)
    with pytest.raises(schedules.ScheduleError):
        schedules.preview("nope", None)


# --------------------------------------------------------------------------- #
# Scheduler
# --------------------------------------------------------------------------- #
def test_tick_fires_a_due_schedule_and_rolls_it_forward(db, conn_factory):
    jm = FakeJobManager()
    sched = Scheduler(conn_factory, jm)
    row = schedules.create(db, schedules.validate(_payload(cron="* * * * *")))
    schedules.set_next_run(db, row["id"], int(time.time()) - 1)

    assert sched.tick() == 1
    assert jm.submitted == [("sync", {}, False, None)]

    after = schedules.get_schedule(db, row["id"])
    assert after["last_status"] == schedules.RUN_SUBMITTED
    assert after["last_job_id"] == "1"
    assert after["next_run_at"] > time.time()
    assert schedules.runs(db, row["id"])[0]["status"] == schedules.RUN_SUBMITTED


def test_tick_does_nothing_when_nothing_is_due(db, conn_factory):
    jm = FakeJobManager()
    schedules.create(db, schedules.validate(_payload()))
    assert Scheduler(conn_factory, jm).tick() == 0
    assert jm.submitted == []


def test_overlapping_run_is_skipped_not_queued(db, conn_factory):
    jm = FakeJobManager()
    jm.jobs.append({"id": "99", "name": "sync", "state": "running"})
    sched = Scheduler(conn_factory, jm)
    row = schedules.create(db, schedules.validate(_payload(cron="* * * * *")))
    schedules.set_next_run(db, row["id"], int(time.time()) - 1)

    assert sched.tick() == 0
    assert jm.submitted == []
    after = schedules.get_schedule(db, row["id"])
    assert after["last_status"] == schedules.RUN_SKIPPED
    # It still moves on: a skip must not leave the schedule stuck due forever.
    assert after["next_run_at"] > time.time()


def test_a_full_queue_is_recorded_as_a_skip(db, conn_factory):
    jm = FakeJobManager()
    jm.raise_with = QueueFullError("job queue is full (5 in flight)")
    sched = Scheduler(conn_factory, jm)
    row = schedules.create(db, schedules.validate(_payload(cron="* * * * *")))
    schedules.set_next_run(db, row["id"], int(time.time()) - 1)

    sched.tick()
    after = schedules.get_schedule(db, row["id"])
    assert after["last_status"] == schedules.RUN_SKIPPED
    assert "full" in schedules.runs(db, row["id"])[0]["detail"]


def test_scheduler_never_passes_a_confirm_phrase(db, conn_factory):
    """The structural half of "a schedule cannot satisfy a typed-phrase gate"."""
    jm = FakeJobManager()
    sched = Scheduler(conn_factory, jm)
    row = schedules.create(
        db,
        schedules.validate(
            _payload(
                job="apply",
                cron="* * * * *",
                params={"dry_run": False, "kinds": ["assign"]},
                confirm=True,
            )
        ),
    )
    schedules.set_next_run(db, row["id"], int(time.time()) - 1)
    sched.tick()
    name, params, confirm, phrase = jm.submitted[0]
    assert name == "apply" and confirm is True and phrase is None


def test_missed_occurrences_are_not_backfilled(db, conn_factory):
    jm = FakeJobManager()
    row = schedules.create(db, schedules.validate(_payload(cron="0 3 * * *")))
    # Two days ago: the server was down.
    schedules.set_next_run(db, row["id"], int(time.time()) - 2 * 86400)

    Scheduler(conn_factory, jm)._bootstrap()

    assert jm.submitted == []
    after = schedules.get_schedule(db, row["id"])
    assert after["last_status"] == schedules.RUN_MISSED
    assert after["next_run_at"] > time.time()


def test_a_firing_missed_by_seconds_still_runs(db, conn_factory):
    jm = FakeJobManager()
    sched = Scheduler(conn_factory, jm)
    row = schedules.create(db, schedules.validate(_payload(cron="0 3 * * *")))
    schedules.set_next_run(db, row["id"], int(time.time()) - 10)

    sched._bootstrap()
    assert schedules.get_schedule(db, row["id"])["last_status"] is None
    assert sched.tick() == 1


def test_bootstrap_fills_in_a_missing_next_run(db, conn_factory):
    row = schedules.create(db, schedules.validate(_payload()))
    schedules.set_next_run(db, row["id"], None)
    Scheduler(conn_factory, FakeJobManager())._bootstrap()
    assert schedules.get_schedule(db, row["id"])["next_run_at"] > time.time()


def test_manual_run_does_not_consume_the_next_firing(db, conn_factory):
    jm = FakeJobManager()
    sched = Scheduler(conn_factory, jm)
    row = schedules.create(db, schedules.validate(_payload()))
    before = schedules.get_schedule(db, row["id"])["next_run_at"]

    job_id = sched.fire(db, schedules.get_schedule(db, row["id"]), manual=True)

    assert job_id == "1"
    assert schedules.get_schedule(db, row["id"])["next_run_at"] == before


def test_start_and_stop_are_idempotent(conn_factory):
    sched = Scheduler(conn_factory, FakeJobManager(), tick=0.05)
    sched.start()
    sched.start()
    sched.stop()
    sched.stop()
