"""Extraction throughput benchmark: time the detect -> align -> embed hot path.

Read-only twin of :mod:`synopticon.pipeline.runner`. It reuses the same
detector, ensemble and crop helpers so the numbers reflect a real ``extract``
run, but persists nothing (no ``faces``/``embeddings`` rows, no crop files) and
never touches the NAS beyond fetching originals to measure against.

A configurable warmup pass runs first and is excluded from the reported totals:
the first ONNX inference eats one-off session build + thread-pool spin-up cost
that would otherwise dominate a small sample and misrepresent steady-state speed.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter

import numpy as np

from ..config import Settings
from . import align
from .embed.ensemble import EmbeddingEnsemble
from .runner import CompositeDetector, FetchOriginal, _aligned_crop, load_image_bgr

log = logging.getLogger(__name__)

# Compute stages, in pipeline order. ``fetch`` (NAS download) is timed too but
# kept out of this tuple so throughput reflects local compute, not the network.
_STAGES = ("load", "detect", "align", "embed")


@dataclass
class BenchmarkStats:
    photos: int = 0
    faces: int = 0
    warmup_photos: int = 0
    fetch_s: float = 0.0
    stage_s: dict[str, float] = field(default_factory=lambda: {s: 0.0 for s in _STAGES})

    @property
    def compute_s(self) -> float:
        return sum(self.stage_s.values())

    def _fmt_rate(self, n: int) -> str:
        c = self.compute_s
        return f"{n / c:.2f}/s" if c > 0 else "n/a"

    def __str__(self) -> str:
        if self.photos == 0:
            return "benchmark: no photos measured (nothing synced/downloadable for this space?)"
        c = self.compute_s
        lines = [
            f"benchmark: {self.photos} photos, {self.faces} faces "
            f"(after {self.warmup_photos} warmup)",
            f"  compute: {c:.2f}s total  ->  "
            f"{self._fmt_rate(self.photos)} photos, {self._fmt_rate(self.faces)} faces, "
            f"{1000 * c / self.photos:.0f} ms/photo",
        ]
        for stage in _STAGES:
            s = self.stage_s[stage]
            pct = 100 * s / c if c > 0 else 0.0
            lines.append(f"    {stage:<7} {s:7.2f}s  {pct:5.1f}%  ({1000 * s / self.photos:.0f} ms/photo)")
        lines.append(f"  fetch (NAS download, excluded): {self.fetch_s:.2f}s")
        return "\n".join(lines)


def _fetch_bench_photos(
    conn: sqlite3.Connection, space: str, limit: int, photo_id: int | None
) -> list[sqlite3.Row]:
    if photo_id is not None:
        return conn.execute(
            "SELECT * FROM photos WHERE space = ? AND id = ? AND deleted = 0",
            (space, photo_id),
        ).fetchall()
    # Prefer photos already known to contain faces so the embed stage — the
    # dominant cost — is actually exercised; fall back to id order otherwise.
    return conn.execute(
        "SELECT p.* FROM photos p "
        "LEFT JOIN extract_log e ON e.space = p.space AND e.photo_id = p.id "
        "WHERE p.deleted = 0 AND p.type = 'photo' AND p.space = ? "
        "ORDER BY COALESCE(e.face_count, 0) DESC, p.id "
        "LIMIT ?",
        (space, limit),
    ).fetchall()


def run_benchmark(
    conn: sqlite3.Connection,
    settings: Settings,
    fetch_original: FetchOriginal,
    limit: int = 25,
    photo_id: int | None = None,
    space: str = "personal",
    warmup: int = 2,
    detector_factory=None,
    ensemble_factory=None,
) -> BenchmarkStats:
    """Time the extraction hot path over up to ``limit`` photos (read-only).

    ``fetch_original``, ``detector_factory`` and ``ensemble_factory`` mirror
    :func:`synopticon.pipeline.runner.run_extract` so the CLI wires them
    identically and tests can inject fakes. The first ``warmup`` measured photos
    are processed but excluded from the returned :class:`BenchmarkStats`.
    """
    # A single explicit photo has nothing to spare for warmup.
    if photo_id is not None:
        warmup = 0

    detector = detector_factory() if detector_factory else CompositeDetector.from_settings(
        settings.storage.models_dir, settings
    )
    ensemble = ensemble_factory() if ensemble_factory else EmbeddingEnsemble(
        settings.storage.models_dir, settings.inference
    )

    # Fetch `warmup` extra so the measured sample size still equals `limit`.
    rows = _fetch_bench_photos(conn, space, limit + max(0, warmup), photo_id)
    stats = BenchmarkStats()

    for i, row in enumerate(rows):
        measuring = i >= warmup
        try:
            t = perf_counter()
            path = fetch_original(row)
            fetch = perf_counter() - t
            faces, timings = _bench_photo(path, detector, ensemble)
        except Exception as exc:  # noqa: BLE001 - one bad photo must not abort the run
            log.warning("benchmark: skipping photo %s (space=%s): %s", row["id"], space, exc)
            continue

        if measuring:
            stats.photos += 1
            stats.faces += faces
            stats.fetch_s += fetch
            for stage, secs in timings.items():
                stats.stage_s[stage] += secs
        else:
            stats.warmup_photos += 1

    return stats


def _bench_photo(path: Path, detector, ensemble) -> tuple[int, dict[str, float]]:
    """Run load -> detect -> align -> embed for one photo, timing each stage."""
    timings: dict[str, float] = {}

    t = perf_counter()
    img_bgr = load_image_bgr(path)
    timings["load"] = perf_counter() - t

    t = perf_counter()
    dets = detector.detect(img_bgr)
    timings["detect"] = perf_counter() - t

    t = perf_counter()
    aligned = [_aligned_crop(img_bgr, d) for d in dets]
    for d in dets:
        align.context_crop(img_bgr, d.bbox)  # matched to extract's per-face work
    timings["align"] = perf_counter() - t

    t = perf_counter()
    if aligned:
        ensemble.run(np.stack(aligned))
    timings["embed"] = perf_counter() - t

    return len(dets), timings
