"""The run ledger: CLI recording, job-origin plumbing and the one-shot backfill."""

from __future__ import annotations

import json
import sys

import pytest
from typer.testing import CliRunner

from synopticon import cli, runlog
from synopticon.db import store
from synopticon.web.jobs import JobManager, JobSpec

runner = CliRunner()


@pytest.fixture
def data_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNOPTICON_STORAGE__DATA_DIR", str(tmp_path))
    return tmp_path


def _runs(tmp_path):
    conn = store.connect(tmp_path / "synopticon.db")
    try:
        return [dict(zip(r.keys(), tuple(r))) for r in conn.execute(
            "SELECT * FROM command_runs ORDER BY id"
        ).fetchall()]
    finally:
        conn.close()


def _seed_pending(tmp_path, n=2):
    conn = store.connect(tmp_path / "synopticon.db")
    for _ in range(n):
        conn.execute(
            "INSERT INTO review_queue (kind, payload_json, status, created_at) "
            "VALUES ('assign', '{}', 'pending', ?)",
            (store.now(),),
        )
    conn.commit()
    conn.close()


def test_cli_run_is_recorded_with_stats(data_env):
    _seed_pending(data_env, 2)
    res = runner.invoke(cli.app, ["clear-queue", "-y"])
    assert res.exit_code == 0, res.output
    [run] = _runs(data_env)
    assert run["command"] == "clear-queue"
    assert run["origin"] == "cli"
    assert run["job_id"] is None
    assert run["status"] == "ok"
    assert run["finished_at"] is not None
    assert json.loads(run["stats_json"]) == {"cleared": 2}


def test_failing_command_is_recorded_failed(data_env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("nope")

    monkeypatch.setattr(cli, "_settings", cli._settings)
    _seed_pending(data_env, 1)
    monkeypatch.setattr(store, "describe", boom)
    res = runner.invoke(cli.app, ["clear-queue", "-y"])
    assert res.exit_code != 0
    [run] = _runs(data_env)
    assert run["status"] == "failed"


def test_abort_is_cancelled(data_env):
    _seed_pending(data_env, 1)
    res = runner.invoke(cli.app, ["clear-queue"], input="n\n")
    assert res.exit_code != 0
    [run] = _runs(data_env)
    assert run["status"] == "cancelled"


def test_help_records_nothing(data_env, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["synopticon", "clear-queue", "--help"])
    res = runner.invoke(cli.app, ["clear-queue", "--help"])
    assert res.exit_code == 0
    assert not (data_env / "synopticon.db").exists() or _runs(data_env) == []


def test_origin_and_job_id_from_env(data_env, monkeypatch):
    monkeypatch.setenv("SYNOPTICON_JOB_ORIGIN", "schedule")
    monkeypatch.setenv("SYNOPTICON_JOB_ID", "42")
    runner.invoke(cli.app, ["clear-queue", "-y"])
    [run] = _runs(data_env)
    assert (run["origin"], run["job_id"]) == ("schedule", "42")


def test_unknown_origin_falls_back_to_cli(monkeypatch):
    monkeypatch.setenv("SYNOPTICON_JOB_ORIGIN", "weird")
    assert runlog.origin_from_env() == ("cli", None)


def test_unreachable_ledger_never_changes_the_command(data_env, monkeypatch):
    _seed_pending(data_env, 1)
    real = store.connect
    calls = {"n": 0}

    def flaky(target, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:  # the ledger's start connection
            raise OSError("database unreachable")
        return real(target, *a, **k)

    monkeypatch.setattr(store, "connect", flaky)
    res = runner.invoke(cli.app, ["clear-queue", "-y"])
    assert res.exit_code == 0, res.output
    assert "cleared" in res.output.lower()


# -- job origin ------------------------------------------------------------


def test_job_manager_passes_origin_and_id_to_child(tmp_path):
    script = (
        "import os, json\n"
        "open(os.environ['SYNOPTICON_PROGRESS_FILE'], 'a').write(json.dumps({'v': 1,"
        " 'event': 'result', 'stats': {'origin': os.environ.get('SYNOPTICON_JOB_ORIGIN'),"
        " 'job': os.environ.get('SYNOPTICON_JOB_ID')}}) + '\\n')\n"
    )
    jm = JobManager(
        tmp_path / "jobs",
        specs={"x": JobSpec("x", lambda p: [])},
        command_builder=lambda argv: [sys.executable, "-c", script],
    )
    try:
        import time

        jid = jm.submit("x", origin="schedule")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and jm.get(jid)["state"] not in ("succeeded", "failed"):
            time.sleep(0.05)
        assert jm.get(jid)["origin"] == "schedule"
        events = (tmp_path / "jobs" / jid / "events.jsonl").read_text()
        stats = json.loads(events.splitlines()[-1])["stats"]
        assert stats == {"origin": "schedule", "job": jid}
        assert json.loads((tmp_path / "jobs" / jid / "job.json").read_text())["origin"] == "schedule"
    finally:
        jm.shutdown(timeout=10)


# -- backfill --------------------------------------------------------------


def _job(jobs, jid, **meta):
    d = jobs / jid
    d.mkdir(parents=True)
    base = {"id": jid, "name": "sync", "argv": ["sync"], "state": "succeeded",
            "created_at": 1000.5, "started_at": 1001.5, "ended_at": 1100.2}
    base.update(meta)
    (d / "job.json").write_text(json.dumps(base))
    return d


def test_backfill_imports_idempotently(web_conn, tmp_path):
    jobs = tmp_path / "jobs"
    d = _job(jobs, "1")
    (d / "events.jsonl").write_text(
        json.dumps({"event": "result", "stats": {"a": 1}}) + "\nnot json\n"
        + json.dumps({"event": "result", "stats": {"a": 2}}) + "\n"
    )
    _job(jobs, "2", name="models-download", argv=[], state="failed", origin="schedule")
    _job(jobs, "3", state="running", ended_at=None)
    _job(jobs, "4", state="cancelled", argv=["extract"])
    (jobs / "5").mkdir()
    (jobs / "5" / "job.json").write_text("{broken")
    web_conn.execute(
        "INSERT INTO command_runs (command, origin, job_id, started_at, status) "
        "VALUES ('sync', 'gui', '4', 1, 'ok')"
    )
    web_conn.commit()

    assert runlog.backfill_from_jobs(web_conn, jobs) == 3
    assert runlog.backfill_from_jobs(web_conn, jobs) == 0

    rows = {r["job_id"]: r for r in web_conn.execute("SELECT * FROM command_runs").fetchall()}
    assert set(rows) == {"1", "2", "3", "4"}
    assert (rows["1"]["command"], rows["1"]["status"], rows["1"]["started_at"], rows["1"]["finished_at"]) == (
        "sync", "ok", 1001, 1100)
    assert json.loads(rows["1"]["stats_json"]) == {"a": 2}
    assert (rows["2"]["command"], rows["2"]["status"], rows["2"]["origin"]) == ("models", "failed", "schedule")
    assert (rows["3"]["status"], rows["3"]["finished_at"]) == ("running", None)
    assert rows["4"]["command"] == "sync"  # pre-existing row untouched


def test_backfill_missing_jobs_dir(web_conn, tmp_path):
    assert runlog.backfill_from_jobs(web_conn, tmp_path / "nope") == 0
