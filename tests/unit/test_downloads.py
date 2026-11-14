"""Originals cache: eviction stays within budget while a run is still going."""

from __future__ import annotations

import pytest

from synopticon.config import load_settings
from synopticon.sync import downloads

_FILE = 1000  # bytes per fake original


def _settings(tmp_path, budget_bytes: int, keep: bool = False):
    return load_settings(
        storage={
            "data_dir": tmp_path,
            "originals_cache_gb": budget_bytes / 1e9,
            "keep_originals": keep,
        }
    )


@pytest.fixture
def fake_download(monkeypatch):
    """`ensure_original` writes a fixed-size file instead of hitting the NAS."""
    downloaded: list[int] = []

    def ensure_original(conn, client, settings, row):
        path = downloads.original_path(settings, row)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x" * _FILE)
            downloaded.append(int(row["id"]))
        return path

    monkeypatch.setattr(downloads, "ensure_original", ensure_original)
    return downloaded


def _row(pid: int) -> dict:
    return {"id": pid, "space": "personal", "cache_key": f"ck{pid}", "filename": f"{pid}.jpg"}


def _cache_bytes(settings) -> int:
    return sum(p.stat().st_size for p in settings.storage.originals_dir.rglob("*") if p.is_file())


def test_cache_stays_near_budget_during_the_run(tmp_path, fake_download):
    settings = _settings(tmp_path, budget_bytes=10 * _FILE)
    fetch = downloads.BudgetedFetch(None, None, settings)
    peak = 0
    for pid in range(1, 101):
        assert fetch(_row(pid)).exists()
        peak = max(peak, _cache_bytes(settings))
    # Budget, plus one eviction slice (a tenth of it), plus the file just fetched.
    assert peak <= 10 * _FILE + _FILE + _FILE
    assert fetch.evicted["evicted_files"] > 0


def test_finish_trims_to_budget_and_totals_the_whole_run(tmp_path, fake_download):
    settings = _settings(tmp_path, budget_bytes=10 * _FILE)
    fetch = downloads.BudgetedFetch(None, None, settings)
    for pid in range(1, 51):
        fetch(_row(pid))
    totals = fetch.finish()
    assert _cache_bytes(settings) <= 10 * _FILE
    assert totals["evicted_files"] == 50 - len(list(settings.storage.originals_dir.rglob("*.jpg")))


def test_the_original_just_fetched_survives_an_eviction_it_triggers(tmp_path, fake_download):
    # A budget smaller than one file: every download triggers a pass that wants it gone.
    settings = _settings(tmp_path, budget_bytes=_FILE // 2)
    fetch = downloads.BudgetedFetch(None, None, settings)
    for pid in range(1, 6):
        assert fetch(_row(pid)).exists()


def test_cache_hits_do_not_count_toward_the_next_eviction(tmp_path, fake_download, monkeypatch):
    settings = _settings(tmp_path, budget_bytes=100 * _FILE)
    calls = []
    real_evict = downloads.evict_originals
    monkeypatch.setattr(
        downloads, "evict_originals", lambda *a, **kw: calls.append(1) or real_evict(*a, **kw)
    )
    fetch = downloads.BudgetedFetch(None, None, settings)
    fetch(_row(1))
    for _ in range(50):
        fetch(_row(1))
    assert fake_download == [1]
    assert calls == []


def test_keep_originals_never_evicts(tmp_path, fake_download):
    settings = _settings(tmp_path, budget_bytes=2 * _FILE, keep=True)
    fetch = downloads.BudgetedFetch(None, None, settings)
    for pid in range(1, 21):
        fetch(_row(pid))
    assert fetch.finish() == {"evicted_files": 0, "freed_bytes": 0}
    assert _cache_bytes(settings) == 20 * _FILE
