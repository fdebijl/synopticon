"""Shared data layer for the review queue.

Pure ``sqlite3`` + :class:`~synopticon.config.Settings` — **no fastapi/jinja2
import** so it works without the ``[review]`` extra. Both the legacy review app
(``review/app.py``) and the upcoming web GUI build their review views on top of
these helpers.

The functions here shape ``review_queue`` rows into the item dicts the UI needs
(crop URLs, Synology deep links, merge exemplar thumbnails, reassign target
crops, derived booleans) and mutate only ``review_queue``. Applying decisions to
the NAS is a separate concern (``syno/writeback.py``).

Caching note: ``hidden_persons`` and ``person_faces`` describe data that is
static while a review session runs, but they are *not* cached here — each call
rebuilds from the DB. Callers that want per-lifetime caching (as the legacy app
does) build them once and pass them back into :func:`load_review_items` via the
``hidden`` / ``person_face_map`` parameters.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from ..config import Settings
from ..db import store

# review_queue.kind values (kept as literals to avoid importing the syno layer;
# mirrors schema.sql and syno/writeback.py).
MERGE_NAMED_KIND = "merge_named"

_DECISIONS = {"approve": "approved", "reject": "rejected"}

# Statuses a human decision may be reverted from. Never touch applied/failed
# (the NAS write already happened / was attempted) or pending (nothing to undo).
_UNDOABLE = frozenset({"approved", "rejected"})


# --------------------------------------------------------------------------- #
# Crop URLs
# --------------------------------------------------------------------------- #
def crop_url(crop_path: str | None, crops_dir: Path) -> str | None:
    """Map a stored crop path to a ``/crops/...`` URL, or ``None``.

    The stored ``crop_path`` may be absolute or CWD-relative, and when relative
    it already includes the crops_dir prefix (the runner stores the full path).
    Resolve both to absolute and take the path relative to ``crops_dir``, which
    is what the ``/crops`` static mount serves.
    """
    if not crop_path:
        return None
    try:
        rel = os.path.relpath(Path(crop_path).resolve(), crops_dir.resolve())
    except ValueError:  # e.g. different drive on Windows
        return None
    if rel.startswith(".."):  # outside the served crops dir
        return None
    return "/crops/" + rel.replace(os.sep, "/")


def face_crops(conn: sqlite3.Connection, settings: Settings) -> dict[int, str | None]:
    """Map every ``face_id`` to its ``/crops/...`` URL (or ``None``)."""
    crops_dir = Path(settings.storage.crops_dir)
    return {
        int(r["face_id"]): crop_url(r["crop_path"], crops_dir)
        for r in conn.execute("SELECT face_id, crop_path FROM faces")
    }


# --------------------------------------------------------------------------- #
# Synology Photos deep links
# --------------------------------------------------------------------------- #
def syno_web_base(settings: Settings) -> str | None:
    """Base for Synology Photos web-UI deep links, or None if unconfigured."""
    base = (settings.nas.web_url or settings.nas.url).strip()
    return base.rstrip("/") or None


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


def _linked_photo_id(conn: sqlite3.Connection, payload: dict) -> Any:
    """`payload["photo_id"]` resolved through its similar-group top pick, if any.

    A grouped photo's own timeline item never resolves (Synology's grouped
    view only surfaces the top pick), so ``item_url`` must be built against
    the top pick instead. Passes through unchanged (including ``None``) for
    ungrouped photos or when ``space``/``photo_id`` is missing.
    """
    space, photo_id = payload.get("space"), payload.get("photo_id")
    if not space or photo_id is None:
        return photo_id
    return store.link_photo_id(conn, space, photo_id)


# --------------------------------------------------------------------------- #
# Ground-truth lookups (static during a review session; caller may cache)
# --------------------------------------------------------------------------- #
def hidden_persons(conn: sqlite3.Connection) -> set[tuple[str, int]]:
    """``(space, person_id)`` pairs that are hidden on the NAS (persons.show=0)."""
    return {
        (r["space"], int(r["id"]))
        for r in conn.execute(
            "SELECT space, id FROM persons WHERE show = 0 AND deleted = 0"
        )
    }


def person_faces(
    conn: sqlite3.Connection, settings: Settings
) -> dict[tuple[str, int], list[int]]:
    """``(space, person_id)`` -> our face_ids labeled to that person.

    Best quality first. Built from ground truth (faces/syno_faces), used for
    reassign target-person thumbnails.
    """
    from ..cluster.crossref import label_faces

    quality = {
        int(r["face_id"]): r["quality"] or 0.0
        for r in conn.execute("SELECT face_id, quality FROM faces")
    }
    by_person: dict[tuple[str, int], list[int]] = {}
    for fid, pk in label_faces(conn, settings).items():
        by_person.setdefault(pk, []).append(fid)
    for fids in by_person.values():
        fids.sort(key=lambda f: quality.get(f, 0.0), reverse=True)
    return by_person


def _is_hidden(hidden: set[tuple[str, int]], space: Any, person_id: Any) -> bool:
    if not space or person_id is None:
        return False
    return (space, int(person_id)) in hidden


def _target_crops(
    person_face_map: dict[tuple[str, int], list[int]],
    payload: dict,
    crops: dict[int, str | None],
    limit: int = 3,
) -> list[str]:
    space, person_id = payload.get("space"), payload.get("person_id")
    if not space or person_id is None:
        return []
    out = []
    for fid in person_face_map.get((space, int(person_id)), []):
        url = crops.get(fid)
        if url:
            out.append(url)
        if len(out) >= limit:
            break
    return out


def _merge_side_crops(
    person: dict | None, exemplars: dict, crops: dict[int, str | None], limit: int = 3
) -> list[str]:
    """Up to ``limit`` crop URLs for a merge side, keyed by ``space:person_id``."""
    if not person:
        return []
    key = f"{person.get('space')}:{person.get('person_id')}"
    out = []
    for fid in exemplars.get(key, []):
        url = crops.get(int(fid))
        if url:
            out.append(url)
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------- #
# Read queries
# --------------------------------------------------------------------------- #
def _where(kind: str, status: str) -> tuple[str, list[Any]]:
    clauses, args = [], []
    if status:
        clauses.append("status = ?")
        args.append(status)
    if kind:
        clauses.append("kind = ?")
        args.append(kind)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, args


def count_review_items(
    conn: sqlite3.Connection, kind: str = "", status: str = "pending"
) -> int:
    """Total ``review_queue`` rows matching ``kind``/``status`` (no pagination)."""
    where, args = _where(kind, status)
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM review_queue {where}", args
    ).fetchone()
    return int(row["n"])


def load_review_items(
    conn: sqlite3.Connection,
    settings: Settings,
    kind: str = "",
    status: str = "pending",
    limit: int = 100,
    offset: int = 0,
    *,
    crops: dict[int, str | None] | None = None,
    hidden: set[tuple[str, int]] | None = None,
    person_face_map: dict[tuple[str, int], list[int]] | None = None,
) -> list[dict]:
    """Shape ``review_queue`` rows into UI item dicts, with real pagination.

    ``crops``, ``hidden`` and ``person_face_map`` are optional precomputed
    lookups (see :func:`face_crops`, :func:`hidden_persons`,
    :func:`person_faces`); when omitted they are built from the DB on each call.
    Callers that keep a per-session cache pass their cached copies in.
    """
    web_base = syno_web_base(settings)
    if crops is None:
        crops = face_crops(conn, settings)
    if hidden is None:
        hidden = hidden_persons(conn)
    if person_face_map is None:
        person_face_map = person_faces(conn, settings)

    where, args = _where(kind, status)
    rows = conn.execute(
        f"SELECT item_id, kind, payload_json, confidence, status "
        f"FROM review_queue {where} ORDER BY item_id LIMIT ? OFFSET ?",
        [*args, int(limit), int(offset)],
    ).fetchall()

    items = []
    for r in rows:
        payload = json.loads(r["payload_json"])
        exemplars = (payload.get("evidence") or {}).get("exemplars", {})
        person_a = payload.get("person_a") or {}
        person_b = payload.get("person_b") or {}
        items.append(
            {
                "item_id": r["item_id"],
                "kind": r["kind"],
                "confidence": r["confidence"],
                "status": r["status"],
                "payload": payload,
                "crop": crops.get(int(payload["face_id"]))
                if payload.get("face_id") is not None
                else None,
                "item_url": item_url(
                    web_base, payload.get("space"), _linked_photo_id(conn, payload)
                ),
                "person_a_url": person_url(
                    web_base, person_a.get("space"), person_a.get("person_id")
                ),
                "person_b_url": person_url(
                    web_base, person_b.get("space"), person_b.get("person_id")
                ),
                "person_url": person_url(
                    web_base, payload.get("space"), payload.get("person_id")
                ),
                "from_person_url": person_url(
                    web_base, payload.get("space"), payload.get("from_person_id")
                ),
                "new_person_crops": [
                    crops.get(int(f)) for f in payload.get("face_ids", [])
                ],
                "merge_crops_a": _merge_side_crops(
                    payload.get("person_a"), exemplars, crops
                ),
                "merge_crops_b": _merge_side_crops(
                    payload.get("person_b"), exemplars, crops
                ),
                "unnamed_target": r["kind"] == "reassign"
                and not str(payload.get("person_name") or "").strip(),
                "unnamed_merge": r["kind"] == "merge"
                and not str(person_a.get("name") or "").strip()
                and not str(person_b.get("name") or "").strip(),
                "named_merge": r["kind"] == "merge_named",
                "target_crops": _target_crops(person_face_map, payload, crops)
                if r["kind"] == "reassign"
                else [],
                "target_hidden": _is_hidden(
                    hidden, payload.get("space"), payload.get("person_id")
                ),
                "person_a_hidden": _is_hidden(
                    hidden, person_a.get("space"), person_a.get("person_id")
                ),
                "person_b_hidden": _is_hidden(
                    hidden, person_b.get("space"), person_b.get("person_id")
                ),
            }
        )
    return items


def queue_counts(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    """Nested ``{status: {kind: count}}`` over the whole ``review_queue``."""
    out: dict[str, dict[str, int]] = {}
    for row in conn.execute(
        "SELECT status, kind, COUNT(*) AS n FROM review_queue GROUP BY status, kind"
    ):
        out.setdefault(row["status"], {})[row["kind"]] = int(row["n"])
    return out


def named_merge_pairs(conn: sqlite3.Connection) -> list[dict]:
    """Approved ``merge_named`` rows with both person names/ids.

    Mirrors the named->named warning payload that ``cli.py``'s ``apply-all``
    prints, so consent previews can list the same pairs.
    """
    out = []
    for row in conn.execute(
        "SELECT item_id, payload_json FROM review_queue "
        "WHERE status = 'approved' AND kind = ? ORDER BY item_id",
        (MERGE_NAMED_KIND,),
    ):
        payload = json.loads(row["payload_json"])
        a = payload.get("person_a") or {}
        b = payload.get("person_b") or {}
        out.append(
            {
                "item_id": row["item_id"],
                "person_a": {"person_id": a.get("person_id"), "name": a.get("name")},
                "person_b": {"person_id": b.get("person_id"), "name": b.get("name")},
                "label_a": a.get("name") or a.get("person_id"),
                "label_b": b.get("name") or b.get("person_id"),
            }
        )
    return out


# --------------------------------------------------------------------------- #
# Mutations (review_queue only)
# --------------------------------------------------------------------------- #
def decide_item(
    conn: sqlite3.Connection, item_id: int, decision: str
) -> str | None:
    """Approve/reject a queue item. Returns the new status, or ``None`` if the
    decision is not ``approve``/``reject`` (DB untouched)."""
    status = _DECISIONS.get(decision)
    if status is None:
        return None
    conn.execute(
        "UPDATE review_queue SET status = ?, decided_at = ?, decided_by = ? "
        "WHERE item_id = ?",
        (status, store.now(), "review-ui", item_id),
    )
    conn.commit()
    return status


def undo_decision(conn: sqlite3.Connection, item_id: int) -> str | None:
    """Revert an approve/reject back to ``pending``.

    Resets ``status`` to ``pending`` and clears ``decided_at``/``decided_by`` —
    but **only** when the item currently sits at ``approved`` or ``rejected``.
    Returns the new status (``"pending"``) on success, or ``None`` (DB
    untouched) when the item is missing or in a non-undoable state
    (``applied``/``failed``/``pending``), so the API can signal a conflict.
    """
    row = conn.execute(
        "SELECT status FROM review_queue WHERE item_id = ?", (item_id,)
    ).fetchone()
    if row is None or row["status"] not in _UNDOABLE:
        return None
    conn.execute(
        "UPDATE review_queue SET status = 'pending', decided_at = NULL, "
        "decided_by = NULL WHERE item_id = ?",
        (item_id,),
    )
    conn.commit()
    return "pending"


def bulk_approve(
    conn: sqlite3.Connection, kind: str, min_confidence: float = 0.0
) -> int:
    """Approve all pending rows of ``kind`` at/above ``min_confidence``.

    Returns the number of rows approved.
    """
    cur = conn.execute(
        "UPDATE review_queue SET status = 'approved', decided_at = ?, "
        "decided_by = 'review-ui' WHERE kind = ? AND status = 'pending' "
        "AND confidence IS NOT NULL AND confidence >= ?",
        (store.now(), kind, min_confidence),
    )
    conn.commit()
    return cur.rowcount


def set_suggested_name(
    conn: sqlite3.Connection, item_id: int, name: str
) -> bool:
    """Set ``suggested_name`` on a ``new_person`` item.

    Returns ``False`` (DB untouched) if the item is missing or not a
    ``new_person`` item; ``True`` on success.
    """
    row = conn.execute(
        "SELECT payload_json, kind FROM review_queue WHERE item_id = ?",
        (item_id,),
    ).fetchone()
    if row is None or row["kind"] != "new_person":
        return False
    payload: dict[str, Any] = json.loads(row["payload_json"])
    payload["suggested_name"] = name
    conn.execute(
        "UPDATE review_queue SET payload_json = ? WHERE item_id = ?",
        (json.dumps(payload), item_id),
    )
    conn.commit()
    return True
