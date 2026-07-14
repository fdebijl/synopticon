"""CLI wiring assertions that don't need a NAS or DB."""

from __future__ import annotations

import inspect

from synopticon import cli


def test_apply_defaults_include_assign_and_low_confidence():
    # assign and low_confidence are the same reviewer-approved face assignment
    # (they differ only in the pipeline's original confidence), so both must
    # apply by default. merge stays out — it is irreversible and separately
    # gated by --apply-merges.
    default = inspect.signature(cli.apply).parameters["kinds"].default.default
    kinds = {k.strip() for k in default.split(",")}
    assert kinds == {"assign", "low_confidence"}
