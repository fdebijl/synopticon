"""CLI wiring assertions that don't need a NAS or DB."""

from __future__ import annotations

import inspect
import json

import pytest
from typer.testing import CliRunner

from synopticon import cli
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
