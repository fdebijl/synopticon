"""Core MCP tools: run_job scopes, update_config guards, review decisions, crops."""

from __future__ import annotations

import json

import pytest

from synopticon.config import load_settings
from synopticon.db import store
from synopticon.web.jobs import ConsentError, JobParamError, QueueFullError
from synopticon.web.mcp import registry, tools_core
from synopticon.web.mcp.registry import McpToken, ToolContext, ToolError, ToolOutput

ALL_SCOPES = frozenset(s.id for s in registry.SCOPES)


class FakeJobManager:
    def __init__(self):
        self.submitted: list[tuple] = []
        self.jobs: list[dict] = []
        self.raise_on_submit: Exception | None = None
        self.cancelled: list[str] = []

    def submit(self, name, params=None, *, confirm=False, confirm_phrase=None, origin="gui"):
        if self.raise_on_submit:
            raise self.raise_on_submit
        self.submitted.append((name, params, confirm, confirm_phrase, origin))
        return str(len(self.submitted))

    def list_jobs(self):
        return self.jobs

    def history(self, limit=50):
        return self.jobs[:limit]

    def get(self, job_id):
        return {"id": job_id, "name": "extract", "state": "running", "seq": 3} if job_id == "7" else None

    def events(self, job_id, after=0):
        return [{"seq": i, "event": "log", "message": "x" * 5000} for i in range(1, 4) if i > after]

    def cancel(self, job_id):
        self.cancelled.append(job_id)
        return job_id == "7"


class FakeVoided:
    def __init__(self, ids=()):
        self.ids = frozenset(ids)

    def get(self, conn):
        return self.ids


@pytest.fixture
def settings(tmp_path):
    return load_settings(storage={"data_dir": tmp_path})


@pytest.fixture
def db(settings):
    c = store.connect(settings.storage.db_path)
    yield c
    c.close()


@pytest.fixture
def jm():
    return FakeJobManager()


def _ctx(settings, jm, scopes=ALL_SCOPES, voided=()):
    return ToolContext(
        settings=settings,
        conn=lambda: store.connect(settings.storage.db_path),
        token=McpToken(id=1, name="agent", scopes=frozenset(scopes) | {"read"}),
        job_manager=jm,
        voided=FakeVoided(voided),
    )


def _run(ctx, job, params=None):
    return tools_core.run_job.handler(ctx, {"job": job, "params": params or {}})


# -- run_job: scope matrix ------------------------------------------------------


@pytest.mark.parametrize(
    "job,params",
    [
        ("sync", {}),
        ("extract", {"limit": 5}),
        ("recluster", {"overrides": {"clustering.edge_threshold": 0.6}}),
        ("apply", {}),  # dry-run preview
        ("apply", {"dry_run": True, "kinds": ["reassign", "merge"]}),
        ("dedupe", {"exact": True}),
    ],
)
def test_jobs_scope_runs_safe_jobs_and_previews(settings, jm, job, params):
    result = _run(_ctx(settings, jm, {"jobs"}), job, params)
    assert result["job_id"] == "1"
    assert "--apply" not in result["argv"]


def test_read_only_token_cannot_run_jobs(settings, jm):
    with pytest.raises(registry.ScopeError) as exc:
        _run(_ctx(settings, jm, set()), "sync")
    assert exc.value.scope == "jobs"
    assert jm.submitted == []


@pytest.mark.parametrize("job", ["clear-queue", "clear-applies", "prune-queue", "delete-crops", "reset"])
def test_maintenance_jobs_need_maintenance_not_jobs(settings, jm, job):
    with pytest.raises(registry.ScopeError) as exc:
        _run(_ctx(settings, jm, {"jobs", "review", "config", "apply"}), job)
    assert exc.value.scope == "maintenance"
    result = _run(_ctx(settings, jm, {"maintenance"}), job)
    assert "-y" in result["argv"]


def test_apply_needs_apply_scope(settings, jm):
    params = {"dry_run": False, "kinds": ["assign"]}
    with pytest.raises(registry.ScopeError) as exc:
        _run(_ctx(settings, jm, {"jobs", "maintenance"}), "apply", params)
    assert exc.value.scope == "apply"
    result = _run(_ctx(settings, jm, {"apply"}), "apply", params)
    assert "--apply" in result["argv"]
    assert "--apply-reassigns" not in result["argv"] and "--apply-merges" not in result["argv"]


def test_apply_reassign_needs_its_own_scope_and_flag(settings, jm):
    params = {"dry_run": False, "kinds": ["reassign"]}
    with pytest.raises(ToolError, match="apply_reassigns"):
        _run(_ctx(settings, jm, ALL_SCOPES), "apply", params)
    params["apply_reassigns"] = True
    with pytest.raises(registry.ScopeError) as exc:
        _run(_ctx(settings, jm, {"apply"}), "apply", params)
    assert exc.value.scope == "apply:reassign"
    result = _run(_ctx(settings, jm, {"apply", "apply:reassign"}), "apply", params)
    assert "--apply-reassigns" in result["argv"]


def test_apply_merge_needs_its_own_scope_and_flag(settings, jm):
    params = {"dry_run": False, "kinds": ["merge"], "apply_merges": True}
    with pytest.raises(registry.ScopeError) as exc:
        _run(_ctx(settings, jm, {"apply", "apply:reassign"}), "apply", params)
    assert exc.value.scope == "apply:merge"
    result = _run(_ctx(settings, jm, {"apply", "apply:merge"}), "apply", params)
    assert "--apply-merges" in result["argv"]


def test_typed_phrase_operations_are_refused_even_with_every_scope(settings, jm):
    ctx = _ctx(settings, jm, ALL_SCOPES)
    cases = [
        ("apply", {"dry_run": False, "kinds": ["merge_named"]}),
        ("apply", {"dry_run": False, "kinds": ["merge_named", "assign"], "apply_merges": True}),
        ("dedupe", {"exact": True, "apply": True}),
        ("reset", {"all": True}),
    ]
    for job, params in cases:
        with pytest.raises(ToolError, match="only a human"):
            _run(ctx, job, params)
    assert jm.submitted == []


def test_no_forbidden_flags_ever_reach_the_manager(settings, jm):
    ctx = _ctx(settings, jm, ALL_SCOPES)
    _run(ctx, "apply", {"dry_run": False, "kinds": ["assign", "reassign", "merge"], "apply_reassigns": True, "apply_merges": True})
    _run(ctx, "reset", {})
    for name, params, confirm, phrase, origin in jm.submitted:
        assert phrase is None
        assert confirm is True
        assert origin == "mcp"


def test_submit_passes_origin_mcp_and_no_phrase(settings, jm):
    _run(_ctx(settings, jm, {"jobs"}), "sync", {"space": "personal"})
    assert jm.submitted == [("sync", {"space": "personal"}, True, None, "mcp")]


def test_unknown_job_and_bad_params_are_tool_errors(settings, jm):
    ctx = _ctx(settings, jm, {"jobs"})
    with pytest.raises(ToolError, match="unknown job"):
        _run(ctx, "eval")
    with pytest.raises(ToolError):
        _run(ctx, "recluster", {"overrides": {"nas.url": "x"}})
    with pytest.raises(ToolError):
        tools_core.run_job.handler(ctx, {"job": "sync", "params": []})


def test_queue_full_is_a_tool_error(settings, jm):
    jm.raise_on_submit = QueueFullError("job queue is full (5 in flight)")
    with pytest.raises(ToolError, match="queue is full"):
        _run(_ctx(settings, jm, {"jobs"}), "sync")


def test_run_job_is_listed_for_jobs_maintenance_apply_only():
    assert tools_core.run_job.scopes == {"jobs", "maintenance", "apply"}
    assert tools_core.update_config.scopes == {"config"}
    assert tools_core.decide_review_items.scopes == {"review"}
    assert tools_core.cancel_job.scopes == {"jobs"}


def test_cancel_job(settings, jm):
    ctx = _ctx(settings, jm, {"jobs"})
    assert tools_core.cancel_job.handler(ctx, {"job_id": "7"})["cancelled"] is True
    with pytest.raises(ToolError, match="unknown or already finished"):
        tools_core.cancel_job.handler(ctx, {"job_id": "8"})


# -- job reads ---------------------------------------------------------------------


def test_get_job_events_cursor_and_clipping(settings, jm):
    ctx = _ctx(settings, jm, set())
    out = tools_core.get_job.handler(ctx, {"job_id": "7"})
    assert [e["seq"] for e in out["events"]] == [1, 2, 3]
    assert out["next_after"] == 3
    assert len(out["events"][0]["message"]) < 2100
    out = tools_core.get_job.handler(ctx, {"job_id": "7", "after": 1, "limit": 1})
    assert [e["seq"] for e in out["events"]] == [2]
    assert out["next_after"] == 2
    out = tools_core.get_job.handler(ctx, {"job_id": "7", "after": 3})
    assert out["events"] == [] and out["next_after"] == 3
    with pytest.raises(ToolError, match="unknown job"):
        tools_core.get_job.handler(ctx, {"job_id": "nope"})


# -- update_config -------------------------------------------------------------------


@pytest.fixture
def cfg(tmp_path, settings, jm):
    (tmp_path / "config.toml").write_text(
        "[nas]\nurl = \"https://nas.test\"\n\n[clustering]\nedge_threshold = 0.5\n"
    )
    return _ctx(load_settings(storage={"data_dir": tmp_path}), jm, {"config"})


def test_update_config_writes_and_reports_before_after(tmp_path, cfg):
    out = tools_core.update_config.handler(cfg, {"changes": {"clustering": {"edge_threshold": 0.6}}})
    assert out["changed"] == {"clustering.edge_threshold": {"before": 0.5, "after": 0.6}}
    assert "edge_threshold = 0.6" in (tmp_path / "config.toml").read_text()
    assert "https://nas.test" in (tmp_path / "config.toml").read_text()


@pytest.mark.parametrize("section", ["nas", "storage", "database", "security", "mcp", "bogus"])
def test_update_config_refuses_non_allowlisted_sections(cfg, section):
    with pytest.raises(ToolError, match="not writable"):
        tools_core.update_config.handler(cfg, {"changes": {section: {"x": 1}}})


def test_update_config_refuses_mcp_enable_and_leaves_file_alone(tmp_path, cfg):
    before = (tmp_path / "config.toml").read_text()
    with pytest.raises(ToolError):
        tools_core.update_config.handler(cfg, {"changes": {"mcp": {"enabled": True}}})
    assert (tmp_path / "config.toml").read_text() == before


def test_update_config_requires_config_scope(settings, jm):
    with pytest.raises(registry.ScopeError):
        tools_core.update_config.handler(_ctx(settings, jm, set()), {"changes": {"clustering": {"seed": 1}}})


def test_update_config_unknown_key_and_invalid_value(cfg):
    with pytest.raises(ToolError, match="unknown key clustering.nope"):
        tools_core.update_config.handler(cfg, {"changes": {"clustering": {"nope": 1}}})
    with pytest.raises(ToolError, match="invalid settings"):
        tools_core.update_config.handler(cfg, {"changes": {"clustering": {"edge_threshold": "high"}}})


def test_update_config_refused_while_a_job_runs(tmp_path, cfg, jm):
    jm.jobs = [{"id": "1", "state": "running"}]
    before = (tmp_path / "config.toml").read_text()
    with pytest.raises(ToolError, match="job is queued or running"):
        tools_core.update_config.handler(cfg, {"changes": {"clustering": {"edge_threshold": 0.6}}})
    assert (tmp_path / "config.toml").read_text() == before


def test_update_config_pipeline_version_change_needs_confirm_redetect(tmp_path, cfg, monkeypatch):
    monkeypatch.setattr(
        tools_core, "_pipeline_version", lambda s: f"v{s.detection.min_face_px}"
    )
    changes = {"detection": {"min_face_px": 40}}
    before = (tmp_path / "config.toml").read_text()
    with pytest.raises(ToolError, match="confirm_redetect"):
        tools_core.update_config.handler(cfg, {"changes": changes})
    assert (tmp_path / "config.toml").read_text() == before
    out = tools_core.update_config.handler(cfg, {"changes": changes, "confirm_redetect": True})
    assert out["pipeline_version_before"] != out["pipeline_version_after"]
    assert out["changed"]["detection.min_face_px"]["after"] == 40


def test_update_config_warns_about_env_shadowed_keys(cfg, monkeypatch):
    monkeypatch.setenv("SYNOPTICON_CLUSTERING__SEED", "7")
    out = tools_core.update_config.handler(cfg, {"changes": {"clustering": {"seed": 9}}})
    assert any("clustering.seed" in w for w in out["warnings"])


def test_get_config_is_fresh_masked_and_scoped(tmp_path, cfg):
    (tmp_path / "config.toml").write_text(
        "[nas]\npassword = \"hunter2\"\n\n[clustering]\nedge_threshold = 0.61\n"
    )
    out = tools_core.get_config.handler(cfg, {})
    assert out["config"]["clustering"]["edge_threshold"]["value"] == 0.61
    assert out["config"]["clustering"]["edge_threshold"]["default"] == 0.5
    assert out["config"]["clustering"]["edge_threshold"]["description"]
    assert out["config"]["nas"]["password"]["value"] == {"secret": True, "set": True}
    assert "hunter2" not in json.dumps(out)
    assert out["writable_sections"] == list(tools_core.WRITABLE_SECTIONS)
    only = tools_core.get_config.handler(cfg, {"section": "mcp"})
    assert list(only["config"]) == ["mcp"]
    with pytest.raises(ToolError, match="unknown section"):
        tools_core.get_config.handler(cfg, {"section": "zzz"})


def test_get_config_writable_sections_empty_without_scope(settings, jm):
    out = tools_core.get_config.handler(_ctx(settings, jm, set()), {})
    assert out["writable_sections"] == []


# -- review ----------------------------------------------------------------------------


def _item(db, kind="assign", status="pending", payload=None):
    cur = db.execute(
        "INSERT INTO review_queue (kind, payload_json, confidence, status, created_at) VALUES (?,?,?,?,?)",
        (kind, json.dumps(payload or {"face_id": 1}), 0.9, status, store.now()),
    )
    db.commit()
    return int(cur.lastrowid)


def test_decide_review_items_outcomes(settings, jm, db):
    pending = _item(db)
    applied = _item(db, status="applied")
    gone = _item(db)
    ctx = _ctx(settings, jm, {"review"}, voided={gone})
    out = tools_core.decide_review_items.handler(
        ctx, {"item_ids": [pending, applied, gone, 9999], "decision": "approve"}
    )
    by_id = {o["item_id"]: o["outcome"] for o in out["items"]}
    assert by_id == {
        pending: "ok",
        applied: "skipped_final",
        gone: "skipped_deleted_photo",
        9999: "not_found",
    }
    row = db.execute("SELECT status, decided_by FROM review_queue WHERE item_id = ?", (pending,)).fetchone()
    assert (row["status"], row["decided_by"]) == ("approved", "mcp:agent")
    assert db.execute("SELECT status FROM review_queue WHERE item_id = ?", (gone,)).fetchone()[0] == "pending"
    assert db.execute("SELECT status FROM review_queue WHERE item_id = ?", (applied,)).fetchone()[0] == "applied"


def test_decide_hide_reject_and_undo(settings, jm, db):
    a, b = _item(db), _item(db)
    ctx = _ctx(settings, jm, {"review"})
    tools_core.decide_review_items.handler(ctx, {"item_ids": [a], "decision": "hide"})
    tools_core.decide_review_items.handler(ctx, {"item_ids": [b], "decision": "reject"})
    status = lambda i: db.execute("SELECT status FROM review_queue WHERE item_id = ?", (i,)).fetchone()[0]
    assert (status(a), status(b)) == ("hidden", "rejected")
    out = tools_core.decide_review_items.handler(ctx, {"item_ids": [a, b], "decision": "pending"})
    assert out["tally"] == {"ok": 2}
    assert (status(a), status(b)) == ("pending", "pending")
    out = tools_core.decide_review_items.handler(ctx, {"item_ids": [a], "decision": "pending"})
    assert out["items"][0]["outcome"] == "not_undoable"


def test_decide_review_items_validation_and_scope(settings, jm):
    ctx = _ctx(settings, jm, {"review"})
    with pytest.raises(ToolError):
        tools_core.decide_review_items.handler(ctx, {"item_ids": [], "decision": "approve"})
    with pytest.raises(ToolError):
        tools_core.decide_review_items.handler(ctx, {"item_ids": list(range(501)), "decision": "approve"})
    with pytest.raises(ToolError):
        tools_core.decide_review_items.handler(ctx, {"item_ids": [1], "decision": "delete"})
    with pytest.raises(registry.ScopeError):
        tools_core.decide_review_items.handler(_ctx(settings, jm, set()), {"item_ids": [1], "decision": "approve"})


def test_list_review_items_excludes_voided_and_trims_evidence(settings, jm, db):
    keep = _item(db, payload={"face_id": 4, "evidence": {"exemplars": {"a": [1, 2]}, "votes": 3}})
    gone = _item(db)
    ctx = _ctx(settings, jm, set(), voided={gone})
    out = tools_core.list_review_items.handler(ctx, {})
    assert [i["item_id"] for i in out["items"]] == [keep]
    assert out["total"] == 1
    assert 4 in out["items"][0]["face_ids"]
    assert out["items"][0]["payload"]["evidence"] == {"votes": 3}
    counts = tools_core.get_review_counts.handler(ctx, {})
    assert counts["totals"] == {"pending": 1}


# -- crops -------------------------------------------------------------------------------


def _face(db, face_id, crop_path=None, ctx_path=None):
    db.execute(
        "INSERT INTO faces (face_id, space, photo_id, detector, x, y, w, h, det_score, crop_path, "
        "ctx_crop_path, quality, pipeline_version, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (face_id, "personal", 1, "scrfd", face_id, 0, 10, 10, 0.97, crop_path, ctx_path, 21.5, "v", store.now()),
    )
    db.commit()


def test_get_face_crops_returns_images_with_captions(tmp_path, settings, jm, db):
    crops = settings.storage.crops_dir
    crops.mkdir(parents=True, exist_ok=True)
    (crops / "1.jpg").write_bytes(b"\xff\xd8jpeg")
    _face(db, 1, str(crops / "1.jpg"))
    out = tools_core.get_face_crops.handler(_ctx(settings, jm, set()), {"face_ids": [1, 2]})
    assert isinstance(out, ToolOutput)
    assert len(out.images) == 1
    assert out.images[0].data == b"\xff\xd8jpeg"
    assert "face 1" in out.images[0].caption and "0.97" in out.images[0].caption
    assert out.structured["missing"] == [{"face_id": 2, "reason": "no such face"}]


def test_get_face_crops_refuses_paths_outside_the_crops_dir(tmp_path, settings, jm, db):
    secret = tmp_path / "secret.jpg"
    secret.write_bytes(b"top secret")
    settings.storage.crops_dir.mkdir(parents=True, exist_ok=True)
    _face(db, 1, str(secret))
    _face(db, 2, str(settings.storage.crops_dir / ".." / ".." / secret.name))
    out = tools_core.get_face_crops.handler(_ctx(settings, jm, set()), {"face_ids": [1, 2]})
    assert out.images == []
    assert len(out.structured["missing"]) == 2


def test_get_face_crops_context_column_and_validation(tmp_path, settings, jm, db):
    crops = settings.storage.crops_dir
    crops.mkdir(parents=True, exist_ok=True)
    (crops / "c.png").write_bytes(b"\x89PNG")
    _face(db, 1, None, str(crops / "c.png"))
    ctx = _ctx(settings, jm, set())
    out = tools_core.get_face_crops.handler(ctx, {"face_ids": [1], "context": True})
    assert out.images[0].mime_type == "image/png"
    for bad in ([], list(range(13)), ["1"], [True]):
        with pytest.raises(ToolError):
            tools_core.get_face_crops.handler(ctx, {"face_ids": bad})


# -- subprocess tools never import the inference stack ------------------------------------


def test_environment_and_nas_check_shell_out(settings, jm, monkeypatch):
    calls = []

    class Proc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(cmd, **kw):
        calls.append((cmd, kw["env"]["SYNOPTICON_JOB_ORIGIN"]))
        return Proc()

    monkeypatch.setattr(tools_core.subprocess, "run", fake_run)
    ctx = _ctx(settings, jm, set())
    assert tools_core.check_nas.handler(ctx, {}) == {"exit_code": 0, "output": "ok"}
    env = tools_core.get_environment.handler(ctx, {})
    assert env["output"] == "ok" and "models" in env
    assert [c[0][-1] for c in calls] == ["check", "hwinfo"]
    assert {c[1] for c in calls} == {"mcp"}


def test_get_overview_shape(settings, jm, db):
    _item(db)
    out = tools_core.get_overview.handler(_ctx(settings, jm, {"jobs"}), {})
    assert out["token"] == {"name": "agent", "scopes": ["jobs", "read"]}
    assert out["review_counts"] == {"pending": {"assign": 1}}
    assert out["active_jobs"] == []
    assert "pipeline_version" in out["pipeline"]
