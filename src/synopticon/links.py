"""Synology Photos deep links.

A dependency-free leaf (config only), so every layer that needs to point a human
at a photo or a person — sync, pipeline, review, the web app, the CLI — builds
the same URL from one place.

Photo links must target the *visible* item: a member of a Synology "similar
photo group" has no timeline route of its own, so resolve the id through
:func:`db.store.link_photo_id` (or :func:`review.queries._link_map` for a page)
before calling :func:`item_url`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from .config import Settings


def syno_web_base(settings: "Settings") -> str | None:
    """Base for Synology Photos web-UI deep links, or None if unconfigured."""
    base = (settings.nas.web_url or settings.nas.url or "").strip().rstrip("/")
    return base or None


def person_url(base: str | None, space: str | None, person_id: Any) -> str | None:
    """Synology Photos link to a person's page."""
    if not base or not space or person_id is None:
        return None
    return f"{base}/?launchApp=SYNO.Foto.AppInstance#/person/{space}_space/{person_id}"


def item_url(base: str | None, space: str | None, photo_id: Any) -> str | None:
    """Synology Photos link to a single photo (timeline item)."""
    if not base or not space or photo_id is None:
        return None
    return (
        f"{base}/?launchApp=SYNO.Foto.AppInstance"
        f"#/{space}_space/timeline/item/{photo_id}"
    )
