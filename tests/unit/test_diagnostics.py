"""hwinfo diagnostics must be robust and cover the extraction-relevant sections."""

from __future__ import annotations

from synopticon import diagnostics


def test_render_has_all_sections(tmp_settings):
    out = diagnostics.render(tmp_settings)
    for heading in ("Platform", "CPU", "Memory", "Inference", "Key libraries", "Models", "Storage"):
        assert f"== {heading}" in out or heading in out


def test_collect_is_structured(tmp_settings):
    sections = diagnostics.collect(tmp_settings)
    titles = {title for title, _ in sections}
    assert {"CPU", "Memory", "Models", "Storage"} <= titles
    # every row is a (label, value) string pair
    for _, rows in sections:
        for label, value in rows:
            assert isinstance(label, str) and isinstance(value, str)


def test_models_section_reports_missing_manifest(tmp_settings):
    # tmp_settings points models_dir at the repo default (no manifest under tmp),
    # so an empty/absent manifest must degrade gracefully, not raise.
    _, rows = next(s for s in diagnostics.collect(tmp_settings) if s[0] == "Models")
    assert any(label == "models_dir" for label, _ in rows)


def test_human_bytes():
    assert diagnostics._human_bytes(0) == "0 B"
    assert diagnostics._human_bytes(1536).endswith("KiB")
    assert diagnostics._human_bytes(5 * 1024**3).endswith("GiB")
