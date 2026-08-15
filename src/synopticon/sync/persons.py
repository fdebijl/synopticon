"""Sync person ground truth (`persons`) and per-photo face detail (`syno_faces`)."""

from __future__ import annotations

from collections.abc import Callable

from synopticon.config import Settings, Space
from synopticon.db import Connection, store
from synopticon.syno import foto
from synopticon.syno.client import SynoApiError, SynoClient

_CHUNK = 500
_PROGRESS_EVERY = 50

Progress = Callable[[int, int | None], None]
# Called once per photo we couldn't list faces for: (photo_id, error_code, item_url|None).
SkipCallback = Callable[[int, "int | None", "str | None"], None]


def _item_web_url(settings: Settings, space: Space, photo_id: int) -> str | None:
    """Synology Photos deep link to a single timeline item, or None if no base URL is set.

    Mirrors review.app._item_url — kept local so the sync layer needn't import the
    FastAPI review module.
    """
    base = (settings.nas.web_url or settings.nas.url or "").strip().rstrip("/")
    if not base:
        return None
    return f"{base}/?launchApp=SYNO.Foto.AppInstance#/{space}_space/timeline/item/{photo_id}"


def sync_persons(
    conn: Connection,
    client: SynoClient,
    space: Space,
    progress: Progress | None = None,
) -> dict:
    seen: set[int] = set()
    upserted = 0
    now = store.now()

    for person in foto.list_persons(client, space, show_hidden=True):
        seen.add(person.id)
        conn.execute(
            "INSERT INTO persons (id, space, name, item_count, show, cover, synced_at, deleted) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0) "
            "ON CONFLICT(space, id) DO UPDATE SET "
            "name = excluded.name, item_count = excluded.item_count, show = excluded.show, "
            "cover = excluded.cover, synced_at = excluded.synced_at, deleted = 0",
            (
                person.id, space, person.name, person.item_count,
                None if person.show is None else int(bool(person.show)),
                person.cover, now,
            ),
        )
        upserted += 1
        if progress and upserted % _PROGRESS_EVERY == 0:
            progress(upserted, None)
    conn.commit()
    if progress:
        progress(upserted, None)

    existing_ids = [
        int(row["id"])
        for row in conn.execute(
            "SELECT id FROM persons WHERE space = ? AND deleted = 0", (space,)
        ).fetchall()
    ]
    to_mark = [pid for pid in existing_ids if pid not in seen]
    deleted = 0
    for start in range(0, len(to_mark), _CHUNK):
        chunk = to_mark[start : start + _CHUNK]
        placeholders = ",".join("?" for _ in chunk)
        conn.execute(
            f"UPDATE persons SET deleted = 1 WHERE space = ? AND id IN ({placeholders})",
            (space, *chunk),
        )
        deleted += len(chunk)
    conn.commit()

    return {"seen": len(seen), "upserted": upserted, "deleted": deleted}


def _cursor_key(space: Space) -> str:
    return f"sync_faces_cursor_{space}"


def sync_faces(
    conn: Connection,
    client: SynoClient,
    space: Space,
    only_tagged: bool = True,
    resume: bool = True,
    progress: Progress | None = None,
    on_skip: SkipCallback | None = None,
) -> dict:
    """Per-photo `Browse.Item.list_face` -> `syno_faces` upsert, resumable via `sync_state`.

    Candidate photo ids default to only those already tagged with a person
    (`person_photos`); pass `only_tagged=False` to sweep every non-deleted
    photo instead. The cursor is cleared once a full pass completes.
    """
    if only_tagged:
        # Join photos (not just person_photos) so tags left dangling by a since-deleted
        # or vanished photo are excluded — otherwise list_face errors 117 on them. This
        # mirrors the deleted=0 filter the all-photos branch already applies.
        rows = conn.execute(
            "SELECT DISTINCT pp.photo_id FROM person_photos pp "
            "JOIN photos p ON p.space = pp.space AND p.id = pp.photo_id "
            "WHERE pp.space = ? AND p.deleted = 0 ORDER BY pp.photo_id",
            (space,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id AS photo_id FROM photos WHERE space = ? AND deleted = 0 ORDER BY id",
            (space,),
        ).fetchall()
    candidate_ids = [int(row["photo_id"]) for row in rows]

    cursor_key = _cursor_key(space)
    cursor = store.get_state(conn, cursor_key) if resume else None
    if cursor is not None:
        candidate_ids = [pid for pid in candidate_ids if pid > cursor]

    photos_processed = 0
    faces_upserted = 0
    faces_skipped = 0
    total = len(candidate_ids)
    now = store.now()

    for photo_id in candidate_ids:
        try:
            faces = foto.list_item_faces(client, space, photo_id)
        except SynoApiError as exc:
            # A single unlistable item (deleted/moved/permission-class error such as
            # code 117) must not abort the whole sweep. Advance the cursor past it so
            # resume doesn't wedge on the same photo, and surface it to the caller.
            faces_skipped += 1
            store.set_state(conn, cursor_key, photo_id)
            conn.commit()
            if on_skip is not None:
                linked_id = store.link_photo_id(conn, space, photo_id)
                on_skip(photo_id, exc.code, _item_web_url(client.settings, space, linked_id))
            continue
        conn.execute("DELETE FROM syno_faces WHERE space = ? AND photo_id = ?", (space, photo_id))
        for face in faces:
            x1, y1, x2, y2 = face.bbox.as_tuple()
            conn.execute(
                "INSERT INTO syno_faces (space, syno_face_id, photo_id, person_id, name, "
                "x1, y1, x2, y2, synced_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(space, syno_face_id) DO UPDATE SET "
                "photo_id = excluded.photo_id, person_id = excluded.person_id, "
                "name = excluded.name, x1 = excluded.x1, y1 = excluded.y1, "
                "x2 = excluded.x2, y2 = excluded.y2, synced_at = excluded.synced_at",
                (space, face.face_id, photo_id, face.person_id, face.name, x1, y1, x2, y2, now),
            )
            faces_upserted += 1
        photos_processed += 1
        store.set_state(conn, cursor_key, photo_id)
        conn.commit()
        if progress and photos_processed % _PROGRESS_EVERY == 0:
            progress(photos_processed, total)

    if progress:
        progress(photos_processed, total)

    store.set_state(conn, cursor_key, None)
    conn.commit()

    return {
        "photos_processed": photos_processed,
        "faces_upserted": faces_upserted,
        "faces_skipped": faces_skipped,
    }
