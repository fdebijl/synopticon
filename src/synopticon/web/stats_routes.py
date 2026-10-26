"""Stats page API: ``GET /api/stats/lifetime`` and ``GET /api/stats/library``.

DB-only and NAS-free. Both handlers are sync ``def`` so Starlette runs them in
its threadpool (ADR 07); the library figures are O(library), so they are served
through a fingerprint-keyed :class:`~synopticon.web.stats_page.LibraryCache`.

No ``from __future__ import annotations`` here: FastAPI resolves route
signatures at registration time.
"""

from typing import Callable

from ..config import Settings
from ..db import Connection
from . import stats_page


def register_stats_routes(
    app,
    settings: Settings,
    conn: Callable[[], Connection],
    cache: "stats_page.LibraryCache | None" = None,
) -> stats_page.LibraryCache:
    """Attach the Stats page API to ``app``; returns the library cache it serves from."""
    library_cache = cache if cache is not None else stats_page.LibraryCache()

    @app.get("/api/stats/lifetime")
    def api_stats_lifetime():
        c = conn()
        try:
            return stats_page.lifetime(c)
        finally:
            c.close()

    @app.get("/api/stats/library")
    def api_stats_library():
        c = conn()
        try:
            return library_cache.get(c, settings)
        finally:
            c.close()

    return library_cache
