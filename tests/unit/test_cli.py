"""CLI wiring assertions that don't need a NAS or DB."""

from __future__ import annotations

import inspect
import json
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from synopticon import cli, progress
from synopticon.db import store


def test_apply_defaults_include_assign_and_low_confidence():
    # assign and low_confidence are the same reviewer-approved face assignment
    # (they differ only in the pipeline's original confidence), so both must
    # apply by default. merge stays out — it is irreversible and separately
    # gated by --apply-merges.
    default = inspect.signature(cli.apply).parameters["kinds"].default.default
    kinds = {k.strip() for k in default.split(",")}
    assert kinds == {"assign", "low_confidence"}


# -- apply-all -------------------------------------------------------------


@pytest.fixture
def apply_all_env(monkeypatch, tmp_path, tmp_settings):
    """Patch the NAS/writeback seams so apply-all runs against a temp DB."""
    from synopticon.syno import client as client_mod
    from synopticon.syno import writeback as wb

    conn = store.connect(tmp_path / "synopticon.db")
    for kind in ("assign", "reassign", "merge"):
        conn.execute(
            "INSERT INTO review_queue (kind, payload_json, status, created_at) "
            "VALUES (?, ?, 'approved', ?)",
            (kind, json.dumps({}), store.now()),
        )
    conn.commit()

    calls: dict = {}

    def fake_apply_reviewed(conn, writer, kinds, person_id=None,
                            apply_merges=False, apply_merges_named=False,
                            apply_reassigns=False, **kw):
        calls["kinds"] = set(kinds)
        calls["apply_merges"] = apply_merges
        calls["apply_merges_named"] = apply_merges_named
        calls["apply_reassigns"] = apply_reassigns
        return wb.ApplyStats(considered=3, applied=3)

    class DummyClient:
        def __init__(self, *a, **k): ...
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    monkeypatch.setattr(wb, "apply_reviewed", fake_apply_reviewed)
    monkeypatch.setattr(
        wb, "configure_apply_logging", lambda p, level=20: tmp_path / "apply.log"
    )
    monkeypatch.setattr(client_mod, "SynoClient", DummyClient)
    monkeypatch.setattr(cli, "_settings", lambda: tmp_settings)
    monkeypatch.setattr(cli, "_conn", lambda s: conn)
    return calls


def test_apply_all_lifts_gates_and_covers_all_writable_kinds(apply_all_env):
    result = CliRunner().invoke(cli.app, ["apply-all"], input="y\n")
    assert result.exit_code == 0, result.output
    assert apply_all_env["kinds"] == {
        "assign", "low_confidence", "reassign", "merge", "merge_named"
    }
    assert apply_all_env["apply_merges"] is True
    assert apply_all_env["apply_reassigns"] is True
    # No named->named merge is queued, so its gate stays shut.
    assert apply_all_env["apply_merges_named"] is False
    # The prompt shows per-kind counts before asking.
    assert "1 assign" in result.output
    assert "1 merge" in result.output
    assert "1 reassign" in result.output


def test_apply_all_aborts_on_no(apply_all_env):
    result = CliRunner().invoke(cli.app, ["apply-all"], input="n\n")
    assert result.exit_code != 0
    assert "kinds" not in apply_all_env  # apply_reviewed never called


def test_apply_all_yes_flag_skips_prompt(apply_all_env):
    result = CliRunner().invoke(cli.app, ["apply-all", "-Y"])
    assert result.exit_code == 0, result.output
    assert apply_all_env["apply_merges"] is True
    assert "write 3 item(s) to the NAS?" not in result.output


@pytest.fixture
def apply_all_named_env(apply_all_env, monkeypatch, tmp_path, tmp_settings):
    """apply_all_env plus one approved named->named merge (Alice <-> Bob)."""
    conn = cli._conn(tmp_settings)
    conn.execute(
        "INSERT INTO review_queue (kind, payload_json, status, created_at) "
        "VALUES ('merge_named', ?, 'approved', ?)",
        (
            json.dumps({
                "person_a": {"space": "personal", "person_id": 10, "name": "Alice"},
                "person_b": {"space": "personal", "person_id": 20, "name": "Bob"},
                "evidence": {},
            }),
            store.now(),
        ),
    )
    conn.commit()
    return apply_all_env


def test_apply_all_named_merge_listed_and_gated_by_second_confirm(apply_all_named_env):
    # First prompt gates the named->named merges, second the bulk write.
    result = CliRunner().invoke(cli.app, ["apply-all"], input="y\ny\n")
    assert result.exit_code == 0, result.output
    assert "WARNING" in result.output
    assert "Alice (id 10)" in result.output and "Bob (id 20)" in result.output
    assert apply_all_named_env["apply_merges_named"] is True
    assert "merge_named" in apply_all_named_env["kinds"]


def test_apply_all_named_merge_declined_still_applies_rest(apply_all_named_env):
    # Decline the named merges, accept the rest: gate stays shut, others proceed.
    result = CliRunner().invoke(cli.app, ["apply-all"], input="n\ny\n")
    assert result.exit_code == 0, result.output
    assert apply_all_named_env["apply_merges_named"] is False
    assert apply_all_named_env["apply_merges"] is True


def test_apply_all_yes_skips_named_merges_without_flag(apply_all_named_env):
    result = CliRunner().invoke(cli.app, ["apply-all", "-Y"])
    assert result.exit_code == 0, result.output
    assert apply_all_named_env["apply_merges_named"] is False
    assert "skipping named->named merges" in result.output


def test_apply_all_yes_with_flag_applies_named_merges(apply_all_named_env):
    result = CliRunner().invoke(cli.app, ["apply-all", "-Y", "--apply-merges-named"])
    assert result.exit_code == 0, result.output
    assert apply_all_named_env["apply_merges_named"] is True


# -- progress protocol wiring ----------------------------------------------


def test_sync_emits_progress_events_when_env_set(
    monkeypatch, tmp_path, tmp_settings
):
    """`synopticon sync` with SYNOPTICON_PROGRESS_FILE set emits sync.* progress
    events (proving the _progress wiring) plus a final result event; terminal
    output is otherwise unchanged."""
    from synopticon.sync import items as items_mod
    from synopticon.sync import persons as persons_mod
    from synopticon.syno import client as client_mod

    conn = store.connect(tmp_path / "synopticon.db")

    def fake_sync_items(conn, client, space, progress=None):
        if progress:
            progress(1, 2)
            progress(2, 2)
        return {"seen": 2, "upserted": 2, "deleted": 0}

    def fake_sync_persons(conn, client, space, progress=None):
        if progress:
            progress(1, 1)
        return {"seen": 1, "upserted": 1, "deleted": 0}

    def fake_sync_similar(conn, client, space, progress=None):
        if progress:
            progress(0, 0)
        return {"groups": 0, "members": 0}

    class DummyClient:
        def __init__(self, *a, **k): ...
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    monkeypatch.setattr(items_mod, "sync_items", fake_sync_items)
    monkeypatch.setattr(items_mod, "sync_similar", fake_sync_similar)
    monkeypatch.setattr(persons_mod, "sync_persons", fake_sync_persons)
    monkeypatch.setattr(client_mod, "SynoClient", DummyClient)
    monkeypatch.setattr(cli, "_settings", lambda: tmp_settings)
    monkeypatch.setattr(cli, "_conn", lambda s: conn)

    events_file = tmp_path / "events.jsonl"
    monkeypatch.setenv(progress.ENV_VAR, str(events_file))

    result = CliRunner().invoke(cli.app, ["sync", "--space", "personal", "--skip-faces"])
    assert result.exit_code == 0, result.output

    events = [json.loads(line) for line in events_file.read_text().splitlines()]
    phases = {e["phase"] for e in events if e["event"] == "phase"}
    assert {"sync.items", "sync.persons"} <= phases
    progress_phases = {e["phase"] for e in events if e["event"] == "progress"}
    assert {"sync.items", "sync.persons"} <= progress_phases
    # Each sync.* progress event carries the space it belongs to.
    assert all(
        e.get("space") == "personal"
        for e in events
        if e["event"] == "progress"
    )
    results = [e for e in events if e["event"] == "result"]
    assert len(results) == 1
    assert results[0]["stats"]["personal.items"] == {"seen": 2, "upserted": 2, "deleted": 0}


# -- reset-password --------------------------------------------------------


@pytest.fixture
def pw_env(monkeypatch, tmp_path, tmp_settings):
    """A temp DB wired into the CLI, holding one web account with a live session."""
    from synopticon.web import auth

    conn = store.connect(tmp_path / "synopticon.db")
    uid = auth.create_user(conn, "admin", "old-pw")
    token = auth.create_session(conn, uid)
    monkeypatch.setattr(cli, "_settings", lambda: tmp_settings)
    monkeypatch.setattr(cli, "_conn", lambda s: conn)
    yield SimpleNamespace(conn=conn, auth=auth, uid=uid, token=token)
    conn.close()


def test_reset_password_sets_new_hash_and_revokes_sessions(pw_env):
    result = CliRunner().invoke(
        cli.app, ["reset-password", "--password", "new-pw"]
    )
    assert result.exit_code == 0, result.output
    assert pw_env.auth.verify_password(pw_env.conn, "admin", "old-pw") is None
    assert pw_env.auth.verify_password(pw_env.conn, "admin", "new-pw") == pw_env.uid
    # The old cookie must not survive an out-of-band reset.
    assert pw_env.auth.validate_session(pw_env.conn, pw_env.token) is None
    assert "revoked 1 active session" in result.output


def test_reset_password_keep_sessions_leaves_cookie_valid(pw_env):
    result = CliRunner().invoke(
        cli.app, ["reset-password", "--password", "new-pw", "--keep-sessions"]
    )
    assert result.exit_code == 0, result.output
    assert pw_env.auth.validate_session(pw_env.conn, pw_env.token) == pw_env.uid


def test_reset_password_prompts_when_password_omitted(pw_env):
    result = CliRunner().invoke(
        cli.app, ["reset-password"], input="typed-pw\ntyped-pw\n"
    )
    assert result.exit_code == 0, result.output
    assert pw_env.auth.verify_password(pw_env.conn, "admin", "typed-pw") == pw_env.uid


def test_reset_password_unknown_username_errors(pw_env):
    result = CliRunner().invoke(
        cli.app, ["reset-password", "nobody", "--password", "x"]
    )
    assert result.exit_code == 1
    assert pw_env.auth.verify_password(pw_env.conn, "admin", "old-pw") == pw_env.uid


def test_reset_password_requires_explicit_name_with_several_accounts(pw_env):
    pw_env.auth.create_user(pw_env.conn, "second", "pw")
    result = CliRunner().invoke(cli.app, ["reset-password", "--password", "x"])
    assert result.exit_code == 1
    assert "several accounts" in result.output
    # Naming one works.
    ok = CliRunner().invoke(cli.app, ["reset-password", "second", "--password", "new-pw"])
    assert ok.exit_code == 0, ok.output
    assert pw_env.auth.verify_password(pw_env.conn, "second", "new-pw") is not None


def test_reset_password_no_accounts_points_at_the_wizard(monkeypatch, tmp_path, tmp_settings):
    conn = store.connect(tmp_path / "synopticon.db")
    monkeypatch.setattr(cli, "_settings", lambda: tmp_settings)
    monkeypatch.setattr(cli, "_conn", lambda s: conn)
    result = CliRunner().invoke(cli.app, ["reset-password", "--password", "x"])
    assert result.exit_code == 1
    assert "setup wizard" in result.output
    conn.close()
