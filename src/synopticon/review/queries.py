"""Shared data layer for the review queue.

Pure :mod:`synopticon.db` + :class:`~synopticon.config.Settings` — **no fastapi/jinja2
import** so it works without the ``[review]`` extra. Both the legacy review app
(``review/app.py``) and the upcoming web GUI build their review views on top of
these helpers.

The functions here shape ``review_queue`` rows into the item dicts the UI needs
(crop URLs, Synology deep links, merge exemplar thumbnails, reassign target
crops, derived booleans) and mutate only ``review_queue``. Applying decisions to
the NAS is a separate concern (``syno/writeback.py``).

Caching note: ``face_crops``, ``hidden_persons`` and ``person_faces`` each scan
the *whole* library, so rebuilding them per call makes
:func:`load_review_items` O(library) rather than O(page). They are deliberately
not cached here — callers pass precomputed copies back in via the ``crops`` /
``hidden`` / ``person_face_map`` parameters. The web GUI does exactly that
through :mod:`synopticon.review.lookups`, which owns the cache and its
invalidation.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from ..db import Connection

from ..config import Settings
from ..db import store
from ..links import (  # noqa: F401 - re-exported
    inspect_path,
    item_url,
    person_url,
    syno_web_base,
)

# review_queue.kind values (kept as literals to avoid importing the syno layer;
# mirrors schema.sql and syno/writeback.py).
MERGE_NAMED_KIND = "merge_named"

# Kinds whose payload names a single target person, so an empty `person_name`
# means the item would tag a face onto a person nobody has named yet.
_TARGETED_KINDS = frozenset({"assign", "low_confidence", "reassign"})

_DECISIONS = {"approve": "approved", "reject": "rejected", "hide": "hidden"}

# Statuses a human decision may be reverted from. Never touch applied/failed
# (the NAS write already happened / was attempted) or pending (nothing to undo).
_UNDOABLE = frozenset({"approved", "rejected", "hidden"})

# Kinds whose target person a human may override from the review UI.
_RETARGETABLE_KINDS = frozenset({"assign", "low_confidence", "new_person"})

# Statuses a retarget refuses. `applied` already reached the NAS, so rewriting
# its target would describe a write that never happened; `hidden` is either a
# deliberate dismissal or a row that has already been merged into someone.
_UNRETARGETABLE = frozenset({"applied", "hidden"})


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


def crop_url_mapper(crops_dir: Path):
    """A ``crop_path -> /crops/... URL`` function that avoids a syscall per crop.

    :func:`crop_url` calls ``Path.resolve()`` on both sides, i.e. a ``realpath``
    syscall for *every* crop. Mapping a whole library (tens of thousands of
    faces) that way costs seconds. Here the root is resolved once and the common
    case — a stored path that is lexically under it — is pure string work. A
    path that does not match lexically (symlinked crops dir, ``..`` segments)
    falls back to the exact :func:`crop_url` semantics, so output is unchanged.
    """
    root = str(crops_dir.resolve())
    prefix = root + os.sep

    def to_url(crop_path: str | None) -> str | None:
        if not crop_path:
            return None
        absolute = os.path.abspath(crop_path)
        if absolute.startswith(prefix):
            return "/crops/" + absolute[len(prefix) :].replace(os.sep, "/")
        return crop_url(crop_path, crops_dir)

    return to_url


def face_crops(conn: Connection, settings: Settings) -> dict[int, str | None]:
    """Map every ``face_id`` to its ``/crops/...`` URL (or ``None``)."""
    to_url = crop_url_mapper(Path(settings.storage.crops_dir))
    return {
        int(r["face_id"]): to_url(r["crop_path"])
        for r in conn.execute("SELECT face_id, crop_path FROM faces")
    }


def _link_map(
    conn: Connection, payloads: list[dict]
) -> dict[tuple[str, int], int]:
    """``(space, photo_id) -> similar-group top pick`` for a whole page.

    A grouped photo's own timeline item never resolves (Synology's grouped view
    only surfaces the top pick), so ``item_url`` must be built against the top
    pick instead — the same resolution :func:`store.link_photo_id` does, but as
    one query per space rather than a round-trip per row. Photos that are
    ungrouped (or missing) are simply absent from the result, so callers fall
    back to the id they already have.
    """
    by_space: dict[str, set[int]] = {}
    for payload in payloads:
        space, photo_id = payload.get("space"), payload.get("photo_id")
        if space and photo_id is not None:
            by_space.setdefault(str(space), set()).add(int(photo_id))
    out: dict[tuple[str, int], int] = {}
    for space, ids in by_space.items():
        id_list = list(ids)
        placeholders = ",".join("?" * len(id_list))
        for row in conn.execute(
            f"SELECT id, similar_top_pick FROM photos "
            f"WHERE space = ? AND id IN ({placeholders})",
            [space, *id_list],
        ):
            if row["similar_top_pick"] is not None:
                out[(space, int(row["id"]))] = int(row["similar_top_pick"])
    return out


# --------------------------------------------------------------------------- #
# Ground-truth lookups (static during a review session; caller may cache)
# --------------------------------------------------------------------------- #
def hidden_persons(conn: Connection) -> set[tuple[str, int]]:
    """``(space, person_id)`` pairs that are hidden on the NAS (persons.show=0)."""
    return {
        (r["space"], int(r["id"]))
        for r in conn.execute(
            "SELECT space, id FROM persons WHERE show = 0 AND deleted = 0"
        )
    }


def person_faces(
    conn: Connection, settings: Settings
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


def _person_crops(
    person_face_map: dict[tuple[str, int], list[int]] | None,
    key: tuple[str, int],
    crops: dict[int, str | None],
    limit: int = 3,
) -> list[str]:
    """Up to ``limit`` crop URLs for one ``(space, person_id)``, best quality first."""
    if not person_face_map:
        return []
    out = []
    for fid in person_face_map.get(key, []):
        url = crops.get(fid)
        if url:
            out.append(url)
        if len(out) >= limit:
            break
    return out


def _target_crops(
    person_face_map: dict[tuple[str, int], list[int]],
    payload: dict,
    crops: dict[int, str | None],
    limit: int = 3,
) -> list[str]:
    space, person_id = payload.get("space"), payload.get("person_id")
    if not space or person_id is None:
        return []
    return _person_crops(person_face_map, (space, int(person_id)), crops, limit)


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
    conn: Connection, kind: str = "", status: str = "pending"
) -> int:
    """Total ``review_queue`` rows matching ``kind``/``status`` (no pagination)."""
    where, args = _where(kind, status)
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM review_queue {where}", args
    ).fetchone()
    return int(row["n"])


def load_review_items(
    conn: Connection,
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

    payloads = [json.loads(r["payload_json"]) for r in rows]
    links = _link_map(conn, payloads)

    items = []
    for r, payload in zip(rows, payloads):
        exemplars = (payload.get("evidence") or {}).get("exemplars", {})
        person_a = payload.get("person_a") or {}
        person_b = payload.get("person_b") or {}
        space, photo_id = payload.get("space"), payload.get("photo_id")
        linked_photo_id = (
            links.get((str(space), int(photo_id)), photo_id)
            if space and photo_id is not None
            else photo_id
        )
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
                "item_url": item_url(web_base, space, linked_photo_id),
                "inspect_url": inspect_path(space, photo_id),
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
                "unnamed_target": r["kind"] in _TARGETED_KINDS
                and not str(payload.get("person_name") or "").strip(),
                "unnamed_merge": r["kind"] == "merge"
                and not str(person_a.get("name") or "").strip()
                and not str(person_b.get("name") or "").strip(),
                "named_merge": r["kind"] == "merge_named",
                "target_crops": _target_crops(person_face_map, payload, crops)
                if r["kind"] == "reassign" or payload.get("manual_target")
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


def person_search(
    conn: Connection,
    q: str,
    *,
    space: str = "",
    limit: int = 10,
    crops: dict[int, str | None] | None = None,
    person_face_map: dict[tuple[str, int], list[int]] | None = None,
    hidden: set[tuple[str, int]] | None = None,
) -> list[dict]:
    """Named persons matching ``q``, for the review UI's retarget picker.

    Searches the **local** ``persons`` mirror rather than
    ``foto.suggest_person``: review is a local-DB surface (it must work with no
    NAS reachable), and this way the target thumbnails are the same
    ``/crops/...`` URLs the cards already show. Unnamed persons are excluded —
    there is no name to search them by, and QuickMerger is the surface for
    those.
    """
    needle = q.strip()
    if not needle:
        return []
    if crops is None:
        crops = {}
    args: list[Any] = [f"%{needle.lower()}%"]
    space_clause = ""
    if space:
        space_clause = "AND space = ? "
        args.append(space)
    args.append(max(1, min(int(limit), 25)))
    rows = conn.execute(
        "SELECT space, id, name, item_count FROM persons "
        "WHERE deleted = 0 AND name IS NOT NULL AND name <> '' "
        f"AND LOWER(name) LIKE ? {space_clause}"
        "ORDER BY item_count DESC, name LIMIT ?",
        args,
    ).fetchall()

    out = []
    for row in rows:
        key = (row["space"], int(row["id"]))
        out.append(
            {
                "space": row["space"],
                "person_id": int(row["id"]),
                "name": row["name"],
                "item_count": row["item_count"],
                "hidden": key in hidden if hidden else False,
                "crops": _person_crops(person_face_map, key, crops),
            }
        )
    return out


def queue_counts(conn: Connection) -> dict[str, dict[str, int]]:
    """Nested ``{status: {kind: count}}`` over the whole ``review_queue``."""
    out: dict[str, dict[str, int]] = {}
    for row in conn.execute(
        "SELECT status, kind, COUNT(*) AS n FROM review_queue GROUP BY status, kind"
    ):
        out.setdefault(row["status"], {})[row["kind"]] = int(row["n"])
    return out


def named_merge_pairs(conn: Connection) -> list[dict]:
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
# Orphan detection
# --------------------------------------------------------------------------- #
def payload_face_ids(payload: dict) -> set[int]:
    """Every ``faces.face_id`` a review payload points at.

    Three shapes across the kinds: a single ``face_id`` (assign, low_confidence,
    reassign, restore_disagreement), a ``face_ids`` list (new_person), and
    ``evidence.exemplars`` — a ``"space:person_id" -> [face_id, ...]`` map on both
    merge kinds. One payload may carry more than one of them, and a payload
    written by an older pipeline may carry a shape that has since changed, so
    every read here is defensive.
    """
    out: set[int] = set()

    def add(value: Any) -> None:
        try:
            out.add(int(value))
        except (TypeError, ValueError):  # a null or a non-numeric leftover
            pass

    if payload.get("face_id") is not None:
        add(payload["face_id"])
    face_ids = payload.get("face_ids")
    if isinstance(face_ids, (list, tuple)):
        for fid in face_ids:
            add(fid)
    evidence = payload.get("evidence")
    exemplars = evidence.get("exemplars") if isinstance(evidence, dict) else None
    if isinstance(exemplars, dict):
        for fids in exemplars.values():
            if isinstance(fids, (list, tuple)):
                for fid in fids:
                    add(fid)
    return out


#: What a referenced face can still contribute to a review card.
FACE_OK = "ok"  #: ``crop_path`` is set — the card renders, and regen replaces a lost file.
FACE_REPAIRABLE = "repairable"  #: no ``crop_path`` yet, but the original is still fetchable.
FACE_LOST = "lost"  #: no ``faces`` row at all, or no crop and no original to rebuild from.

#: ``IN (...)`` batch size for face-id lookups. Well under SQLite's default
#: 999-parameter ceiling, which the whole referenced set would blow past.
_ID_CHUNK = 500


def face_render_state(conn: Connection, face_ids: Iterable[int]) -> dict[int, str]:
    """Classify each ``face_id`` as :data:`FACE_OK` / :data:`FACE_REPAIRABLE` /
    :data:`FACE_LOST`.

    Ids with no ``faces`` row are reported as lost rather than omitted, so a
    caller never has to distinguish "absent" from "classified". This is what
    separates a review item a ``regen-crops`` pass can still fix from one whose
    inputs are gone for good: a crop is rebuildable from ``bbox`` + the original,
    so it needs both the face row *and* a live photo row behind it.
    """
    ids = sorted({int(f) for f in face_ids})
    state = dict.fromkeys(ids, FACE_LOST)
    for start in range(0, len(ids), _ID_CHUNK):
        chunk = ids[start : start + _ID_CHUNK]
        placeholders = ",".join("?" * len(chunk))
        for row in conn.execute(
            "SELECT f.face_id, f.crop_path, p.id AS photo_row, p.deleted AS photo_deleted "
            "FROM faces f LEFT JOIN photos p ON p.space = f.space AND p.id = f.photo_id "
            f"WHERE f.face_id IN ({placeholders})",
            chunk,
        ):
            fid = int(row["face_id"])
            if row["crop_path"]:
                state[fid] = FACE_OK
            elif row["photo_row"] is not None and not row["photo_deleted"]:
                state[fid] = FACE_REPAIRABLE
            else:
                state[fid] = FACE_LOST
    return state


#: Statuses :func:`orphaned_items` targets unless told otherwise. These are the
#: two the next ``cluster`` run re-proposes from scratch, so dropping an orphan
#: loses nothing: ``pending`` was never decided, and ``rejected`` is re-offered by
#: design (see ADR 14 on why ``hidden`` is the status that is *not*). Every other
#: status holds either a human decision or a completed NAS write.
DEFAULT_PRUNE_STATUSES: tuple[str, ...] = ("pending", "rejected")


def orphaned_items(
    conn: Connection, statuses: Sequence[str] = DEFAULT_PRUNE_STATUSES
) -> dict[str, list[int]]:
    """``status -> item_ids`` for queue rows whose faces are all unrecoverable.

    A row is an orphan when it references at least one face and *none* of them
    survive: ``review_queue`` stores raw ``face_id``s in its JSON payload with no
    foreign key, while a re-extract deletes and re-inserts a photo's faces under
    fresh autoincrement ids. Any row proposed before that bump then points at ids
    that no longer exist, so its card renders with no crop and no ``regen-crops``
    pass can bring it back.

    Partial survival is deliberately not an orphan: a merge whose exemplar list
    still resolves in part renders thumbnails and stays reviewable. Nor is a row
    that references no faces at all, since there is nothing to judge it on.
    """
    if not statuses:
        return {}
    placeholders = ",".join("?" * len(statuses))
    rows = conn.execute(
        "SELECT item_id, status, payload_json FROM review_queue "
        f"WHERE status IN ({placeholders}) ORDER BY item_id",
        list(statuses),
    ).fetchall()

    referenced: dict[int, tuple[str, set[int]]] = {}
    every_id: set[int] = set()
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except (json.JSONDecodeError, TypeError):
            # An unparseable payload is unreviewable, but it is also not
            # something a face lookup can speak to. Leave it to a human.
            continue
        if not isinstance(payload, dict):
            continue
        fids = payload_face_ids(payload)
        if not fids:
            continue
        referenced[int(row["item_id"])] = (str(row["status"]), fids)
        every_id |= fids

    state = face_render_state(conn, every_id)
    out: dict[str, list[int]] = {}
    for item_id, (status, fids) in referenced.items():
        if all(state.get(fid, FACE_LOST) == FACE_LOST for fid in fids):
            out.setdefault(status, []).append(item_id)
    return out


#: Every status ``review_queue.status`` can hold (mirrors schema.sql).
ALL_STATUSES: tuple[str, ...] = (
    "pending",
    "approved",
    "rejected",
    "hidden",
    "applied",
    "failed",
)


def orphan_counts(conn: Connection) -> dict[str, int]:
    """``status -> orphan count`` across *every* status.

    Reported wider than :data:`DEFAULT_PRUNE_STATUSES` prunes on purpose: an
    orphaned ``approved`` row still applies to the NAS from its stored payload
    alone (``writeback.apply_reviewed`` never reads ``faces``), so it is a write
    a human green-lit without being able to see it. Surfacing the count is how
    that becomes visible; whether to drop it stays the human's call.
    """
    return {
        status: len(ids)
        for status, ids in orphaned_items(conn, ALL_STATUSES).items()
        if ids
    }


# --------------------------------------------------------------------------- #
# Mutations (review_queue only)
# --------------------------------------------------------------------------- #
def delete_items(conn: Connection, item_ids: Sequence[int]) -> int:
    """Delete queue rows by id, returning how many were removed.

    ``audit_log.review_item_id`` references ``review_queue`` with no cascade, so
    any link to a row being dropped is nulled first or the delete trips the FK —
    the same order ``cli.py``'s ``clear-queue`` uses.
    """
    ids = sorted({int(i) for i in item_ids})
    if not ids:
        return 0
    deleted = 0
    for start in range(0, len(ids), _ID_CHUNK):
        chunk = ids[start : start + _ID_CHUNK]
        placeholders = ",".join("?" * len(chunk))
        conn.execute(
            f"UPDATE audit_log SET review_item_id = NULL WHERE review_item_id IN ({placeholders})",
            chunk,
        )
        cur = conn.execute(
            f"DELETE FROM review_queue WHERE item_id IN ({placeholders})", chunk
        )
        deleted += max(cur.rowcount, 0)
    conn.commit()
    return deleted


def decide_item(
    conn: Connection, item_id: int, decision: str
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


def undo_decision(conn: Connection, item_id: int) -> str | None:
    """Revert an approve/reject back to ``pending``.

    Resets ``status`` to ``pending`` and clears ``decided_at``/``decided_by`` —
    but **only** when the item currently sits at ``approved``, ``rejected`` or
    ``hidden``. Returns the new status (``"pending"``) on success, or ``None``
    (DB untouched) when the item is missing or in a non-undoable state
    (``applied``/``failed``/``pending``), so the API can signal a conflict.

    A row hidden by a *retarget* is also refused: its faces already live in
    approved ``assign`` rows, so restoring the suggestion would offer a decision
    the human has made.
    """
    row = conn.execute(
        "SELECT status, payload_json FROM review_queue WHERE item_id = ?",
        (item_id,),
    ).fetchone()
    if row is None or row["status"] not in _UNDOABLE:
        return None
    if row["status"] == "hidden":
        try:
            if json.loads(row["payload_json"]).get("retargeted_to"):
                return None
        except (json.JSONDecodeError, TypeError):
            pass
    conn.execute(
        "UPDATE review_queue SET status = 'pending', decided_at = NULL, "
        "decided_by = NULL WHERE item_id = ?",
        (item_id,),
    )
    conn.commit()
    return "pending"


def bulk_approve(
    conn: Connection, kind: str, min_confidence: float = 0.0
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


class SpaceMismatch(ValueError):
    """A retarget named a person in a different space than the face lives in."""


def _cluster_face_ids(
    conn: Connection, run_id: Any, exemplars: list[int]
) -> list[int]:
    """Every face in the cluster the ``new_person`` item came from.

    The payload keeps only the first 20 exemplars, so a "these are all P" merge
    has to recover full membership from ``cluster_members`` — seeded from an
    exemplar, since the payload does not carry the cluster id either. Falls back
    to the exemplars when the run is gone (``run_id`` is ``ON DELETE SET NULL``)
    or the members were cleared.
    """
    if run_id is None or not exemplars:
        return list(exemplars)
    rows = conn.execute(
        "SELECT face_id FROM cluster_members WHERE run_id = ? AND cluster_id = "
        "(SELECT cluster_id FROM cluster_members WHERE run_id = ? AND face_id = ?) "
        "ORDER BY face_id",
        (run_id, run_id, exemplars[0]),
    ).fetchall()
    return [int(r["face_id"]) for r in rows] or list(exemplars)


def _face_targets(conn: Connection, face_ids: list[int]) -> dict[int, dict]:
    """``face_id -> {space, photo_id, bbox_normalized}`` for the given faces only.

    Same normalization as ``crossref._load_face_meta``, but keyed on a handful
    of ids instead of scanning every face in the library. Faces whose photo has
    no stored dimensions are omitted — there is no bbox to write without them.
    """
    if not face_ids:
        return {}
    placeholders = ",".join("?" for _ in face_ids)
    out: dict[int, dict] = {}
    for row in conn.execute(
        "SELECT f.face_id, f.space, f.photo_id, f.x, f.y, f.w, f.h, "
        "p.width AS pw, p.height AS ph FROM faces f "
        "JOIN photos p ON p.space = f.space AND p.id = f.photo_id "
        f"WHERE f.face_id IN ({placeholders})",
        list(face_ids),
    ):
        pw, ph = row["pw"], row["ph"]
        if not pw or not ph:
            continue
        out[int(row["face_id"])] = {
            "space": row["space"],
            "photo_id": int(row["photo_id"]),
            "bbox_normalized": [
                float(row["x"]) / pw,
                float(row["y"]) / ph,
                (float(row["x"]) + float(row["w"])) / pw,
                (float(row["y"]) + float(row["h"])) / ph,
            ],
        }
    return out


def retarget_item(
    conn: Connection, item_id: int, space: str, person_id: int, person_name: str = ""
) -> dict | None:
    """Point a suggestion at the person a human picked instead.

    Two shapes, one entry point — both queue-only, so the NAS write still
    happens later through ``apply`` under its own flags:

    * ``assign``/``low_confidence`` — rewrite the payload's target person. The
      stored ``confidence`` was a similarity to the *old* person, so it is
      cleared rather than left to read as an endorsement of the new one.
    * ``new_person`` — the kind ``apply`` cannot write at all. Expand the whole
      cluster into one ``assign`` row per face against the chosen person and
      retire the original row as ``hidden``, with a breadcrumb naming what it
      became.

    Rows land ``approved``: picking the person *is* the decision. Returns
    ``None`` (DB untouched) for a missing item, a kind that has no target to
    retarget, or a status in :data:`_UNRETARGETABLE` — which is also what stops a
    repeated call from expanding the same cluster twice. Raises
    :class:`SpaceMismatch` when the person lives in another space than the face.
    """
    row = conn.execute(
        "SELECT kind, payload_json, run_id, status FROM review_queue "
        "WHERE item_id = ?",
        (item_id,),
    ).fetchone()
    if (
        row is None
        or row["kind"] not in _RETARGETABLE_KINDS
        or row["status"] in _UNRETARGETABLE
    ):
        return None
    kind, payload = row["kind"], json.loads(row["payload_json"])
    now = store.now()

    if kind != "new_person":
        if payload.get("space") and payload["space"] != space:
            raise SpaceMismatch(
                f"that person is in the {space} space "
                f"but the face is in {payload['space']}"
            )
        payload["original_person_id"] = payload.get("person_id")
        payload["person_id"] = person_id
        payload["person_name"] = person_name or None
        payload["manual_target"] = True
        conn.execute(
            "UPDATE review_queue SET payload_json = ?, confidence = NULL, "
            "status = 'approved', decided_at = ?, decided_by = ? WHERE item_id = ?",
            (json.dumps(payload), now, "review-ui", item_id),
        )
        conn.commit()
        return {
            "status": "approved",
            "kind": kind,
            "person_id": person_id,
            "person_name": payload["person_name"],
            "created": 0,
            "skipped": 0,
        }

    exemplars = [int(f) for f in payload.get("face_ids") or []]
    face_ids = _cluster_face_ids(conn, row["run_id"], exemplars)
    targets = _face_targets(conn, face_ids)
    created: list[int] = []
    skipped = 0
    for fid in face_ids:
        meta = targets.get(fid)
        if meta is None or meta["space"] != space:
            skipped += 1
            continue
        cur = conn.execute(
            "INSERT INTO review_queue (run_id, kind, payload_json, confidence, "
            "status, decided_at, decided_by, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                row["run_id"],
                "assign",
                json.dumps(
                    {
                        "face_id": fid,
                        "photo_id": meta["photo_id"],
                        "space": meta["space"],
                        "person_id": person_id,
                        "person_name": person_name or None,
                        "bbox_normalized": meta["bbox_normalized"],
                        "confidence": None,
                        "manual_target": True,
                        "source_item_id": item_id,
                    }
                ),
                None,
                "approved",
                now,
                "review-ui",
                now,
            ),
        )
        created.append(int(cur.lastrowid))

    payload["retargeted_to"] = {
        "space": space,
        "person_id": person_id,
        "person_name": person_name or None,
        "item_ids": created,
    }
    conn.execute(
        "UPDATE review_queue SET payload_json = ?, status = 'hidden', "
        "decided_at = ?, decided_by = ? WHERE item_id = ?",
        (json.dumps(payload), now, "review-ui", item_id),
    )
    conn.commit()
    return {
        "status": "hidden",
        "kind": kind,
        "person_id": person_id,
        "person_name": person_name or None,
        "created": len(created),
        "skipped": skipped,
    }


def set_suggested_name(
    conn: Connection, item_id: int, name: str
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
