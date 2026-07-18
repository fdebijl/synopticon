"""Unit tests for pipeline.manifest's required-models helpers.

These are pure-pathlib, dependency-light checks: ``missing_models`` reports
which required weight files are absent on disk (independent of the manifest
JSON), and the download script's registry must not drift from REQUIRED_MODELS.
"""

from __future__ import annotations

from synopticon.pipeline import manifest as mf


def test_required_models_has_five_keys():
    assert set(mf.REQUIRED_MODELS) == {
        "scrfd_10g_bnkps",
        "yolov8l-face",
        "glintr100",
        "adaface_ir101_webface12m",
        "magface_iresnet100",
    }


def test_missing_models_all_absent_on_fresh_dir(tmp_path):
    assert set(mf.missing_models(tmp_path)) == set(mf.REQUIRED_MODELS)


def test_missing_models_empty_when_all_present(tmp_path):
    for filename in mf.REQUIRED_MODELS.values():
        (tmp_path / filename).write_bytes(b"x")
    assert mf.missing_models(tmp_path) == []


def test_missing_models_reports_only_absent(tmp_path):
    present = {"scrfd_10g_bnkps", "glintr100"}
    for key in present:
        (tmp_path / mf.REQUIRED_MODELS[key]).write_bytes(b"x")
    assert set(mf.missing_models(tmp_path)) == set(mf.REQUIRED_MODELS) - present


def test_download_registry_matches_required_models():
    # Importing the script runs its import-time consistency assert; also compare
    # the maps directly so a drift is reported as data, not just an AssertionError.
    import importlib.util
    from pathlib import Path

    script = Path(__file__).resolve().parents[2] / "scripts" / "download_models.py"
    spec = importlib.util.spec_from_file_location("download_models", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert {k: v["file"] for k, v in module.KNOWN_MODELS.items()} == mf.REQUIRED_MODELS
