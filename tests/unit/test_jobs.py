"""Hermetic tests for the web-GUI subprocess job manager.

Real subprocesses are spawned via a fake JobSpec whose argv drives an inline
``python -c`` script (no NAS, no models, no DB). The consent/argv matrix is
tested directly against the real JOB_SPECS through ``resolve_argv``.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time

import pytest

from synopticon.web.jobs import (
    ConsentError,
    DangerLevel,
    JobManager,
    JobParamError,
    JobSpec,
    QueueFullError,
    resolve_argv,
)

# --------------------------------------------------------------------------- #
# Inline fake-job scripts (run as `python -c <script> <argv...>`)              #
# --------------------------------------------------------------------------- #

# Emits a phase + N progress events (0.1 s apart) then a result. `sys.argv[1]`
# = number of steps, `sys.argv[2]` (optional) = per-step sleep seconds.
_SCRIPT_PROGRESS = r"""
import os, sys, json, time
p = os.environ["SYNOPTICON_PROGRESS_FILE"]
os.makedirs(os.path.dirname(p), exist_ok=True)
def emit(o):
    with open(p, "a") as f:
        f.write(json.dumps(o) + "\n")
n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
nap = float(sys.argv[2]) if len(sys.argv) > 2 else 0.1
emit({"v": 1, "event": "phase", "phase": "test"})
for i in range(n):
    emit({"v": 1, "event": "progress", "phase": "test", "done": i + 1, "total": n})
    time.sleep(nap)
emit({"v": 1, "event": "result", "ok": True})
"""

# Writes one phase event then sleeps effectively forever (until signalled).
_SCRIPT_SLEEP = r"""
import os, sys, json, time
p = os.environ["SYNOPTICON_PROGRESS_FILE"]
os.makedirs(os.path.dirname(p), exist_ok=True)
with open(p, "a") as f:
    f.write(json.dumps({"v": 1, "event": "phase", "phase": "long"}) + "\n")
for _ in range(3000):
    time.sleep(0.1)
"""

# Emits a malformed (non-JSON) line then a valid one.
_SCRIPT_MALFORMED = r"""
import os, json
p = os.environ["SYNOPTICON_PROGRESS_FILE"]
os.makedirs(os.path.dirname(p), exist_ok=True)
with open(p, "a") as f:
    f.write("this is not json\n")
    f.write(json.dumps({"v": 1, "event": "log", "message": "ok"}) + "\n")
"""

# Writes only to the console: a plain stdout line, an in-place redraw sequence, an
# ANSI-coloured stderr warning, and a final line with no trailing newline.
_SCRIPT_CONSOLE = r"""
import sys
print("hello from stdout")
sys.stderr.write("\x1b[33mwatch out\x1b[0m\n")
sys.stderr.write("step: 1/3\rstep: 2/3\rstep: 3/3\n")
sys.stdout.write("no trailing newline")
"""

# Dies with an uncaught exception and emits no structured event at all.
_SCRIPT_CRASH = r"""
print("got as far as here")
raise RuntimeError("model weights not found")
"""

# Reports the resource limits the manager imposed on it: thread-count env vars
# and its own niceness.
_SCRIPT_RESOURCES = r"""
import os, json
p = os.environ["SYNOPTICON_PROGRESS_FILE"]
os.makedirs(os.path.dirname(p), exist_ok=True)
try:
    nice = os.getpriority(os.PRIO_PROCESS, 0)
except (AttributeError, OSError):
    nice = None
with open(p, "a") as f:
    f.write(json.dumps({
        "v": 1, "event": "result",
        "omp": os.environ.get("OMP_NUM_THREADS"),
        "blas": os.environ.get("OPENBLAS_NUM_THREADS"),
        "mkl": os.environ.get("MKL_NUM_THREADS"),
        "nice": nice,
    }) + "\n")
"""


def _builder(script: str):
    return lambda argv: [sys.executable, "-c", script, *argv]


def _fake_specs():
    return {
        "progress": JobSpec("progress", lambda p: [str(p.get("n", 3)), str(p.get("nap", 0.1))]),
        "sleep": JobSpec("sleep", lambda p: []),
        "malformed": JobSpec("malformed", lambda p: []),
        "console": JobSpec("console", lambda p: []),
        "crash": JobSpec("crash", lambda p: []),
        "resources": JobSpec("resources", lambda p: []),
    }


@pytest.fixture
def manager_factory(tmp_path):
    """Yields a factory producing JobManagers that are shut down on teardown."""
    made: list[JobManager] = []

    def make(script: str = _SCRIPT_PROGRESS, **kw):
        jm = JobManager(
            tmp_path / "jobs",
            specs=_fake_specs(),
            command_builder=_builder(script),
            **kw,
        )
        made.append(jm)
        return jm

    yield make
    for jm in made:
        jm.shutdown(timeout=10)


def _wait_state(jm: JobManager, job_id: str, state, timeout=15.0):
    states = {state} if isinstance(state, str) else set(state)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        meta = jm.get(job_id)
        if meta and meta["state"] in states:
            return meta
        time.sleep(0.05)
    meta = jm.get(job_id)
    raise AssertionError(f"job {job_id} never reached {states}; last={meta}")


def _wait(pred, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False


# --------------------------------------------------------------------------- #
# Lifecycle / execution                                                         #
# --------------------------------------------------------------------------- #


def test_job_runs_to_success(manager_factory):
    jm = manager_factory()
    jid = jm.submit("progress", {"n": 3})
    meta = _wait_state(jm, jid, "succeeded")
    assert meta["exit_code"] == 0


def test_serialization_second_job_queued(manager_factory):
    jm = manager_factory(_SCRIPT_SLEEP)
    first = jm.submit("sleep")
    second = jm.submit("sleep")
    assert _wait(lambda: jm.get(first)["state"] == "running")
    # While the first runs, the second must still be queued (FIFO, one worker).
    assert jm.get(second)["state"] == "queued"
    jm.cancel(first)
    assert _wait(lambda: jm.get(second)["state"] == "running")


def test_seq_cursor_and_event_tailing(manager_factory):
    jm = manager_factory()
    jid = jm.submit("progress", {"n": 4})
    _wait_state(jm, jid, "succeeded")
    events = jm.events(jid)
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)  # monotonic, unique
    kinds = [e["event"] for e in events]
    assert "phase" in kinds and kinds.count("progress") == 4
    assert kinds[-1] == "final"  # synthesized terminal event
    # Cursor: everything after the max seq is empty; after 0 is everything.
    assert jm.events(jid, after=max(seqs)) == []
    assert len(jm.events(jid, after=0)) == len(events)


def test_malformed_line_wrapped_as_log(manager_factory):
    jm = manager_factory(_SCRIPT_MALFORMED)
    jid = jm.submit("malformed")
    _wait_state(jm, jid, "succeeded")
    events = jm.events(jid)
    malformed = [e for e in events if e.get("malformed")]
    assert len(malformed) == 1
    assert malformed[0]["event"] == "log" and malformed[0]["message"] == "this is not json"


def _log_messages(jm: JobManager, jid: str) -> list[str]:
    return [e["message"] for e in jm.events(jid) if e["event"] == "log"]


def test_console_output_is_mirrored_as_log_events(manager_factory):
    """stdout/stderr reach the GUI, ANSI-stripped and with `\\r` frames collapsed."""
    jm = manager_factory(_SCRIPT_CONSOLE)
    jid = jm.submit("console")
    _wait_state(jm, jid, "succeeded")
    assert _wait(lambda: len(_log_messages(jm, jid)) >= 4)
    logs = [e for e in jm.events(jid) if e["event"] == "log"]
    by_msg = {e["message"]: e for e in logs}

    assert by_msg["hello from stdout"]["stream"] == "stdout"
    assert by_msg["hello from stdout"]["level"] == "info"
    # ANSI colour codes are stripped, not shown literally.
    assert by_msg["watch out"]["stream"] == "stderr"
    assert by_msg["watch out"]["level"] == "warning"
    # An in-place redraw collapses to its final frame only.
    assert "step: 3/3" in by_msg
    assert "step: 1/3" not in by_msg and "step: 2/3" not in by_msg
    # A trailing line with no newline is still flushed.
    assert "no trailing newline" in by_msg


def test_crash_with_no_events_still_explains_itself(manager_factory):
    """A traceback-only failure yields an `error` event and a job.error headline."""
    jm = manager_factory(_SCRIPT_CRASH)
    jid = jm.submit("crash")
    meta = _wait_state(jm, jid, "failed")
    assert meta["exit_code"] != 0
    # The reason skips the traceback scaffolding and names the exception.
    assert meta["error"] == "RuntimeError: model weights not found"
    assert _wait(lambda: any(e["event"] == "error" for e in jm.events(jid)))
    err = next(e for e in jm.events(jid) if e["event"] == "error")
    assert err["message"] == "RuntimeError: model weights not found"
    # stdout printed before the crash is preserved as context.
    assert "got as far as here" in _log_messages(jm, jid)


def test_error_event_is_not_synthesized_when_command_reported_one(manager_factory, tmp_path):
    """An explicit `error` event is authoritative; no second one is invented."""
    script = r"""
import os, json, sys
p = os.environ["SYNOPTICON_PROGRESS_FILE"]
with open(p, "a") as f:
    f.write(json.dumps({"v": 1, "event": "error", "message": "config invalid"}) + "\n")
sys.exit(1)
"""
    jm = manager_factory(script)
    jm._specs["crash"] = JobSpec("crash", lambda p: [])
    jid = jm.submit("crash")
    _wait_state(jm, jid, "failed")
    assert _wait(lambda: any(e["event"] == "error" for e in jm.events(jid)))
    errors = [e for e in jm.events(jid) if e["event"] == "error"]
    assert [e["message"] for e in errors] == ["config invalid"]


def test_events_replayed_from_disk_for_a_job_this_process_never_ran(manager_factory, tmp_path):
    """A job from a previous server run must not render as an empty log."""
    jm = manager_factory(_SCRIPT_CONSOLE)
    jid = jm.submit("console")
    _wait_state(jm, jid, "succeeded")
    assert _wait(lambda: len(_log_messages(jm, jid)) >= 4)
    live = jm.events(jid)

    # A fresh manager over the same dir has nothing in memory for this job.
    fresh = manager_factory(_SCRIPT_CONSOLE)
    assert jid not in fresh._jobs
    replayed = fresh.events(jid)

    # Same content. Not the same order: the console streams carry no timestamps,
    # so a replay groups stdout then stderr instead of reproducing the live
    # per-poll interleave (documented in JobManager._replay).
    assert sorted(e["message"] for e in replayed if e["event"] == "log") == sorted(
        _log_messages(jm, jid)
    )
    assert replayed[-1]["event"] == "final"
    assert replayed[-1]["state"] == "succeeded"
    assert len(replayed) >= len([e for e in live if e["event"] != "final"])

    # seq is dense and the `after=` cursor drains, exactly like the live path.
    assert [e["seq"] for e in replayed] == list(range(1, len(replayed) + 1))
    assert fresh.events(jid, after=len(replayed)) == []
    assert fresh.get(jid)["seq"] == len(replayed)
    # Repeated reads are served from the cache and stay identical.
    assert fresh.events(jid) == replayed


def test_running_job_listing_carries_a_progress_snapshot(manager_factory):
    """The topbar/dashboard show how far a job got without opening a stream."""
    jm = manager_factory()
    jid = jm.submit("progress", {"n": 6, "nap": 0.25})
    assert _wait(lambda: (jm.get(jid).get("progress") or {}).get("done"))
    snap = jm.get(jid)["progress"]
    assert snap["phase"] == "test"
    assert 1 <= snap["done"] <= 6 and snap["total"] == 6
    assert snap["pct"] == round(snap["done"] * 100 / 6)
    # Present on the listing endpoints too, for jobs this process is running.
    assert next(m for m in jm.history() if m["id"] == jid)["progress"]["phase"] == "test"
    assert next(m for m in jm.list_jobs() if m["id"] == jid)["progress"]["phase"] == "test"

    _wait_state(jm, jid, "succeeded")
    # Never persisted: a stale percentage on disk would outlive the run.
    on_disk = json.loads((jm.jobs_dir / jid / "job.json").read_text())
    assert "progress" not in on_disk


def test_replay_of_unknown_job_is_empty(manager_factory):
    jm = manager_factory()
    assert jm.events("does-not-exist") == []
    assert jm.get("does-not-exist") is None


def test_sigint_cancel_marks_cancelled(manager_factory):
    jm = manager_factory(_SCRIPT_SLEEP)
    jid = jm.submit("sleep")
    assert _wait(lambda: jm.get(jid)["state"] == "running")
    assert jm.cancel(jid) is True
    meta = _wait_state(jm, jid, "cancelled")
    assert meta["state"] == "cancelled"


def test_cancel_queued_job(manager_factory):
    jm = manager_factory(_SCRIPT_SLEEP)
    running = jm.submit("sleep")
    queued = jm.submit("sleep")
    assert _wait(lambda: jm.get(running)["state"] == "running")
    assert jm.cancel(queued) is True
    assert jm.get(queued)["state"] == "cancelled"
    jm.cancel(running)


def test_persistence_job_json_and_events_file(manager_factory, tmp_path):
    jm = manager_factory()
    jid = jm.submit("progress", {"n": 2})
    _wait_state(jm, jid, "succeeded")
    job_dir = tmp_path / "jobs" / jid
    meta = json.loads((job_dir / "job.json").read_text())
    assert meta["state"] == "succeeded" and meta["name"] == "progress"
    lines = (job_dir / "events.jsonl").read_text().strip().splitlines()
    assert any(json.loads(l)["event"] == "result" for l in lines)


def test_job_ids_are_sequential(manager_factory):
    jm = manager_factory()
    first = jm.submit("progress", {"n": 1})
    second = jm.submit("progress", {"n": 1})
    assert first.isdigit() and second.isdigit()
    assert int(second) == int(first) + 1


def test_job_id_counter_survives_restart(manager_factory):
    jm = manager_factory()
    jid = jm.submit("progress", {"n": 1})
    _wait_state(jm, jid, "succeeded")
    jm.shutdown(timeout=10)
    jm2 = manager_factory()
    assert int(jm2.submit("progress", {"n": 1})) == int(jid) + 1


def test_legacy_job_dirs_do_not_inflate_counter(tmp_path, manager_factory):
    # A pre-sequential dir (epoch-ms + uuid fragment) must not seed the counter.
    (tmp_path / "jobs" / "1784394965922-329e207e").mkdir(parents=True)
    jm = manager_factory()
    assert jm.submit("progress", {"n": 1}) == "1"


def test_queue_full_raises(manager_factory):
    jm = manager_factory(_SCRIPT_SLEEP)
    ids = [jm.submit("sleep") for _ in range(5)]  # 1 running + 4 queued = 5 in flight
    assert len(ids) == 5
    with pytest.raises(QueueFullError):
        jm.submit("sleep")


# --------------------------------------------------------------------------- #
# Startup orphan adoption                                                        #
# --------------------------------------------------------------------------- #


def test_dead_pid_orphan_marked_interrupted(tmp_path):
    # A pid that is guaranteed dead (spawned and reaped).
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()

    jobs_dir = tmp_path / "jobs"
    jdir = jobs_dir / "orphan1"
    jdir.mkdir(parents=True)
    (jdir / "job.json").write_text(
        json.dumps(
            {
                "id": "orphan1",
                "name": "sync",
                "params": {},
                "argv": ["sync"],
                "state": "running",
                "created_at": time.time(),
                "pid": dead.pid,
            }
        )
    )
    jm = JobManager(jobs_dir, specs=_fake_specs(), command_builder=_builder(_SCRIPT_SLEEP))
    try:
        assert jm.get("orphan1")["state"] == "interrupted"
        # And it was persisted back to disk.
        assert json.loads((jdir / "job.json").read_text())["state"] == "interrupted"
    finally:
        jm.shutdown(timeout=10)


# --------------------------------------------------------------------------- #
# Consent / argv matrix (real JOB_SPECS via resolve_argv)                       #
# --------------------------------------------------------------------------- #


def test_unknown_job_raises_param_error():
    with pytest.raises(JobParamError):
        resolve_argv("does-not-exist")


def test_safe_commands_need_no_consent():
    assert resolve_argv("cluster") == ["cluster"]
    assert resolve_argv("sync", {"space": "personal", "hash": True}) == [
        "sync", "--space", "personal", "--hash",
    ]
    assert resolve_argv("extract", {"limit": 5}) == ["extract", "--limit", "5"]
    assert resolve_argv("regen-crops", {"only_missing": False}) == ["regen-crops", "--all"]
    assert resolve_argv("models-download", {"only": ["scrfd", "arcface"]}) == [
        "models", "download", "--only", "scrfd", "arcface",
    ]


def test_recluster_whitelists_override_sections():
    argv = resolve_argv("recluster", {"overrides": {"clustering.edge_threshold": 0.47}})
    assert argv == ["recluster", "--set", "clustering.edge_threshold=0.47"]
    with pytest.raises(JobParamError):
        resolve_argv("recluster", {"overrides": {"nas.password": "x"}})
    with pytest.raises(JobParamError):
        resolve_argv("recluster", {"overrides": {"clustering": "x"}})


def test_apply_dry_run_has_no_apply_flag():
    # default (dry_run implied) and explicit dry_run both omit --apply
    for params in ({}, {"dry_run": True}, {"kinds": "assign,merge_named", "dry_run": True}):
        argv = resolve_argv("apply", params)
        assert "--apply" not in argv
        assert not any(a.startswith("--apply") for a in argv)


def test_apply_routine_needs_confirm_only():
    with pytest.raises(ConsentError) as ei:
        resolve_argv("apply", {"dry_run": False, "kinds": "assign,low_confidence"})
    assert ei.value.field == "confirm"

    argv = resolve_argv(
        "apply", {"dry_run": False, "kinds": "assign,low_confidence"}, confirm=True
    )
    assert "--apply" in argv
    assert "--apply-merges" not in argv
    assert "--apply-merges-named" not in argv
    assert "--apply-reassigns" not in argv


def test_apply_reassign_needs_explicit_gate():
    base = {"dry_run": False, "kinds": "reassign"}
    with pytest.raises(ConsentError) as ei:
        resolve_argv("apply", base, confirm=True)
    assert ei.value.field == "apply_reassigns"
    argv = resolve_argv("apply", {**base, "apply_reassigns": True}, confirm=True)
    assert "--apply-reassigns" in argv and "--apply" in argv


def test_apply_merge_gate_never_yields_merge_named():
    base = {"dry_run": False, "kinds": "merge"}
    with pytest.raises(ConsentError) as ei:
        resolve_argv("apply", base, confirm=True)
    assert ei.value.field == "apply_merges"
    argv = resolve_argv("apply", {**base, "apply_merges": True}, confirm=True)
    assert "--apply-merges" in argv
    assert "--apply-merges-named" not in argv  # merge gate must not lift merge_named


def test_apply_merge_named_requires_exact_phrase():
    base = {"dry_run": False, "kinds": "merge_named"}
    with pytest.raises(ConsentError) as ei:
        resolve_argv("apply", base, confirm=True)
    assert ei.value.field == "confirm_phrase" and ei.value.detail == "merge_named"
    # Wrong / near-miss phrases still reject (exact, case-sensitive).
    for bad in ["Merge Named People", "merge named people ", "merge named"]:
        with pytest.raises(ConsentError):
            resolve_argv("apply", base, confirm=True, confirm_phrase=bad)
    # The ConsentError must never leak the expected phrase text.
    assert "merge named people" not in str(ei.value)
    argv = resolve_argv(
        "apply", base, confirm=True, confirm_phrase="merge named people"
    )
    assert "--apply-merges-named" in argv and "--apply" in argv


def test_apply_bad_kind_rejected():
    with pytest.raises(JobParamError):
        resolve_argv("apply", {"kinds": "assign,bogus"})


def test_dedupe_consent_mapping():
    # dry-run is free
    assert resolve_argv("dedupe", {"exact": True}) == ["dedupe", "--exact"]
    # apply requires the phrase
    with pytest.raises(ConsentError):
        resolve_argv("dedupe", {"exact": True, "apply": True})
    argv = resolve_argv(
        "dedupe", {"exact": True, "apply": True}, confirm_phrase="delete duplicates"
    )
    assert "--apply" in argv and "-y" in argv
    # neither exact nor visual -> param error
    with pytest.raises(JobParamError):
        resolve_argv("dedupe", {})


def test_reset_consent_mapping():
    # plain reset needs confirm
    with pytest.raises(ConsentError):
        resolve_argv("reset", {})
    assert resolve_argv("reset", {}, confirm=True) == ["reset", "-y"]
    # reset --all needs the typed phrase, not just confirm
    with pytest.raises(ConsentError) as ei:
        resolve_argv("reset", {"all": True}, confirm=True)
    assert ei.value.detail == "reset_all"
    argv = resolve_argv("reset", {"all": True}, confirm_phrase="reset all")
    assert argv == ["reset", "--all", "-y"]


def test_clear_queue_and_delete_crops_confirm():
    for name in ("clear-queue", "clear-applies", "prune-queue", "delete-crops"):
        with pytest.raises(ConsentError):
            resolve_argv(name)
        assert resolve_argv(name, confirm=True) == [name, "-y"]


def test_prune_queue_include_approved_is_opt_in():
    """Dropping orphans a human already decided on must be asked for explicitly."""
    assert resolve_argv("prune-queue", {}, confirm=True) == ["prune-queue", "-y"]
    assert resolve_argv("prune-queue", {"include_approved": False}, confirm=True) == [
        "prune-queue", "-y",
    ]
    assert resolve_argv("prune-queue", {"include_approved": True}, confirm=True) == [
        "prune-queue", "--include-approved", "-y",
    ]


def test_no_apply_all_or_capital_Y_anywhere():
    combos = [
        ("apply", {"dry_run": False, "kinds": "assign"}, {"confirm": True}),
        ("apply", {"dry_run": False, "kinds": "merge"}, {"confirm": True, }),
        ("dedupe", {"exact": True, "apply": True}, {"confirm_phrase": "delete duplicates"}),
        ("reset", {"all": True}, {"confirm_phrase": "reset all"}),
        ("reset", {}, {"confirm": True}),
        ("clear-queue", {}, {"confirm": True}),
        ("clear-applies", {}, {"confirm": True}),
        ("prune-queue", {"include_approved": True}, {"confirm": True}),
        ("delete-crops", {}, {"confirm": True}),
    ]
    for name, params, kw in combos:
        try:
            argv = resolve_argv(name, params, **kw)
        except ConsentError:
            continue
        assert "apply-all" not in argv
        assert "-Y" not in argv


def test_danger_levels():
    from synopticon.web.jobs import JOB_SPECS

    assert JOB_SPECS["sync"].danger is DangerLevel.SAFE
    assert JOB_SPECS["clear-queue"].danger is DangerLevel.CONFIRM
    assert JOB_SPECS["clear-applies"].danger is DangerLevel.CONFIRM
    assert JOB_SPECS["prune-queue"].danger is DangerLevel.CONFIRM
    assert JOB_SPECS["apply"].danger is DangerLevel.TYPED_PHRASE
    assert JOB_SPECS["dedupe"].danger is DangerLevel.TYPED_PHRASE
    assert JOB_SPECS["reset"].danger is DangerLevel.TYPED_PHRASE


def test_credential_commands_are_never_web_jobs():
    """ADR 05: a command that rewrites a credential or lifts a protection must
    not be reachable from the GUI -- an already-authenticated session could
    otherwise strip the protection it was supposed to be behind."""
    from synopticon.web.jobs import JOB_SPECS
    from synopticon.web import schedules

    forbidden = {"reset-password", "db-migrate", "eval",
                 "disable-2fa", "web-access", "session-pin"}
    assert not (forbidden & set(JOB_SPECS))
    assert not (forbidden & set(schedules.SCHEDULABLE))


# --------------------------------------------------------------------------- #
# Child resource limits                                                        #
# --------------------------------------------------------------------------- #


def _resources_of(jm, job_id):
    meta = _wait_state(jm, job_id, "succeeded")
    assert meta
    result = next(
        e for e in jm.events(job_id) if e.get("event") == "result"
    )
    return result


def test_job_subprocess_gets_a_thread_cap_and_niceness(manager_factory):
    """A job must not be able to take the machine away from the GUI.

    Unconstrained, a clustering run's BLAS pool spawns one busy-spinning thread
    per core; the single uvicorn process then needs tens of seconds of
    wall-clock for a millisecond of work, which from the client side is
    indistinguishable from the server having hung.
    """
    jm = manager_factory(_SCRIPT_RESOURCES, thread_cap=3, nice=7)
    res = _resources_of(jm, jm.submit("resources", {}))

    assert res["omp"] == "3"
    assert res["blas"] == "3"
    assert res["mkl"] == "3"
    if res["nice"] is not None:  # POSIX only
        assert res["nice"] == 7


def test_thread_cap_defaults_to_leaving_the_server_a_core(manager_factory):
    import os

    jm = manager_factory(_SCRIPT_RESOURCES)
    res = _resources_of(jm, jm.submit("resources", {}))

    assert res["omp"] == str(max(1, (os.cpu_count() or 2) - 1))


def test_explicit_thread_env_wins_over_the_cap(manager_factory, monkeypatch):
    """An operator who exported OMP_NUM_THREADS meant it."""
    monkeypatch.setenv("OMP_NUM_THREADS", "11")
    jm = manager_factory(_SCRIPT_RESOURCES, thread_cap=3)
    res = _resources_of(jm, jm.submit("resources", {}))

    assert res["omp"] == "11"
    assert res["blas"] == "3"  # untouched vars still get the cap


def test_thread_cap_zero_leaves_the_environment_alone(manager_factory, monkeypatch):
    monkeypatch.delenv("OMP_NUM_THREADS", raising=False)
    jm = manager_factory(_SCRIPT_RESOURCES, thread_cap=0, nice=0)
    res = _resources_of(jm, jm.submit("resources", {}))

    assert res["omp"] is None
    assert res["blas"] is None
