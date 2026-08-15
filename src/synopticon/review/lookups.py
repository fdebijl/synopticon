"""Process-level cache for the three whole-library review lookups.

:func:`~synopticon.review.queries.load_review_items` needs three maps that are
each derived from the *entire* library rather than from the page being rendered:

* ``crops`` — every ``face_id`` -> ``/crops/...`` URL,
* ``hidden`` — the ``(space, person_id)`` pairs hidden on the NAS,
* ``person_face_map`` — ``(space, person_id)`` -> our face ids, which runs the
  full IoU ground-truth match in :func:`synopticon.cluster.crossref.label_faces`.

Rebuilding them per request makes ``/api/review/items`` cost O(library) instead
of O(page): on a 56k-face library that is ~2.7 s of CPU for a 100-item page, paid
again on every infinite-scroll fetch. They are cached here instead.

**What may not be cached is the invalidation.** The source tables (``photos``,
``faces``, ``syno_faces``, ``person_photos``, ``persons``) are never written by
the web process — only by ``sync``/``extract`` job subprocesses — so a wall-clock
TTL would be both too eager (a review session with no jobs running would keep
paying the rebuild) and too lax. Instead the cache key is a cheap aggregate
fingerprint over exactly those tables (~6 ms), which changes when a job mutates
them and, crucially, does *not* change when a review decision writes to
``review_queue``. Approving an item therefore does not throw the cache away.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from ..db import Connection, errors as db_errors

from ..config import Settings

#: Counts/extents over every table the cached lookups read. Sums are included
#: for the mutable-in-place columns (``persons.show`` flips on a sync,
#: ``syno_faces.person_id`` changes when a face is relabelled) that a plain
#: row count would miss.
_FINGERPRINT_SQL = """
SELECT
  (SELECT COUNT(*) FROM faces),
  (SELECT MAX(face_id) FROM faces),
  (SELECT COUNT(*) FROM syno_faces),
  (SELECT MAX(syno_face_id) FROM syno_faces),
  (SELECT COALESCE(SUM(person_id), 0) FROM syno_faces),
  (SELECT COUNT(*) FROM person_photos),
  (SELECT COUNT(*) FROM persons),
  (SELECT COALESCE(SUM(show), 0) FROM persons),
  (SELECT COALESCE(SUM(deleted), 0) FROM persons),
  (SELECT COUNT(*) FROM photos),
  (SELECT MAX(id) FROM photos)
"""


def fingerprint(conn: Connection) -> tuple:
    """Cheap change-detector over the tables the cached lookups derive from."""
    try:
        return tuple(conn.execute(_FINGERPRINT_SQL).fetchone())
    except db_errors.DatabaseError:
        # A table missing (fresh DB mid-migration) means "don't trust the cache".
        # The rollback is what lets the caller keep using this connection:
        # PostgreSQL aborts the transaction on a failed statement. No-op on SQLite.
        conn.rollback()
        return (None,)


@dataclass(frozen=True)
class ReviewLookups:
    """The three precomputed maps ``load_review_items`` accepts."""

    crops: dict[int, str | None]
    hidden: set[tuple[str, int]]
    person_face_map: dict[tuple[str, int], list[int]]


class LookupCache:
    """Fingerprint-keyed cache of :class:`ReviewLookups`, safe across threads.

    The rebuild happens under the lock on purpose: concurrent requests after an
    invalidation should wait for one rebuild rather than each run their own
    (which is how a single scroll burst turns into N seconds of duplicated CPU).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._key: Any = None
        self._value: ReviewLookups | None = None

    def get(self, conn: Connection, settings: Settings) -> ReviewLookups:
        key = fingerprint(conn)
        with self._lock:
            if self._value is not None and key == self._key and key != (None,):
                return self._value
            value = self._build(conn, settings)
            self._key, self._value = key, value
            return value

    def invalidate(self) -> None:
        with self._lock:
            self._key, self._value = None, None

    @staticmethod
    def _build(conn: Connection, settings: Settings) -> ReviewLookups:
        from . import queries

        return ReviewLookups(
            crops=queries.face_crops(conn, settings),
            hidden=queries.hidden_persons(conn),
            person_face_map=queries.person_faces(conn, settings),
        )
