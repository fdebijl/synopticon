"""hwinfo diagnostics must be robust and cover the extraction-relevant sections."""

from __future__ import annotations

import sys
import types

from synopticon import diagnostics


def _fake_ort(monkeypatch, providers):
    mod = types.ModuleType("onnxruntime")
    mod.__version__ = "1.27.0"
    mod.get_available_providers = lambda: list(providers)
    monkeypatch.setitem(sys.modules, "onnxruntime", mod)


def _inference_rows(settings):
    _, rows = next(s for s in diagnostics.collect(settings) if s[0].startswith("Inference"))
    return dict(rows)


def test_gpu_present_but_cpu_ort_gets_actionable_hint(tmp_settings, monkeypatch):
    monkeypatch.setattr(diagnostics, "_nvidia_gpus", lambda: ["NVIDIA Test, 8192 MiB, 1.0"])
    _fake_ort(monkeypatch, ["CPUExecutionProvider"])
    status = _inference_rows(tmp_settings)["GPU acceleration"]
    assert "CPU-only onnxruntime" in status and "gpu extra" in status


def test_gpu_available_status(tmp_settings, monkeypatch):
    monkeypatch.setattr(diagnostics, "_nvidia_gpus", lambda: ["NVIDIA Test"])
    _fake_ort(monkeypatch, ["CUDAExecutionProvider", "CPUExecutionProvider"])
    assert _inference_rows(tmp_settings)["GPU acceleration"] == "available (CUDA)"


def test_no_gpu_plain_cpu_status(tmp_settings, monkeypatch):
    monkeypatch.setattr(diagnostics, "_nvidia_gpus", lambda: [])
    _fake_ort(monkeypatch, ["CPUExecutionProvider"])
    assert _inference_rows(tmp_settings)["GPU acceleration"] == "not available (CPU only)"


def test_broken_ort_import_reported_as_unknown(tmp_settings, monkeypatch):
    # A mismatched onnxruntime-gpu (missing CUDA libs) fails to import entirely;
    # setting the module to None makes `import onnxruntime` raise ImportError.
    monkeypatch.setattr(diagnostics, "_nvidia_gpus", lambda: ["NVIDIA Test"])
    monkeypatch.setitem(sys.modules, "onnxruntime", None)
    rows = _inference_rows(tmp_settings)
    assert "failed to import" in rows["GPU acceleration"]


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
