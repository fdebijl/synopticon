"""Stats page figures: what Synopticon has done, and what the library is.

Pure :mod:`synopticon.db` + :class:`~synopticon.config.Settings`, NAS-free and
read-only, like :mod:`synopticon.web.stats`. Nothing here may import
``pipeline.runner`` or anything that pulls numpy/cv2 (ADR 07).

Every date bucket is computed in Python: ``strftime`` is not translated for
PostgreSQL (ADR 09). ``photos.time`` is Synology's local wall-clock stored as if
it were UTC, so it is bucketed in UTC to recover the local hour; ``command_runs``
timestamps are real epochs and are bucketed in the server's local time.

Unlisted persons (hidden, deleted or unnamed — ADR 19/20) never appear by name:
their faces are only ever counted, in ``unnamed_or_unlisted_faces``.
"""

from __future__ import annotations

import json
import threading
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any

from ..config import Settings
from ..db import Connection, errors as db_errors, store
from ..links import inspect_path, person_url, syno_web_base

_EPOCH = date(1970, 1, 1)
_MIN_DAY = (date.min - _EPOCH).days
_MAX_DAY = (date.max - _EPOCH).days
_RUN_STATUSES = ("ok", "failed", "cancelled")
_ORIGINS = ("cli", "gui", "schedule")
_PHOTO_TYPES = ("photo", "video", "live")
_PER_PHOTO_BUCKETS = ("0", "1", "2", "3", "4", "5–9", "10+")
_DISTRIBUTION_TOP = 15

#: A face is in the library unless its photo is marked deleted. A face with no
#: photo row at all is kept, as ``load_fused`` does (ADR 18).
_FACE_LIVE = (
    "NOT EXISTS (SELECT 1 FROM photos p "
    "WHERE p.space = f.space AND p.id = f.photo_id AND p.deleted = 1)"
)


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _int(value: Any) -> int:
    return int(value) if value is not None else 0


def _share(part: float, whole: float) -> float:
    return part / whole if whole else 0.0


# --------------------------------------------------------------------------- #
# Lifetime
# --------------------------------------------------------------------------- #
def _empty_command() -> dict[str, Any]:
    return {
        "total": 0, "ok": 0, "failed": 0, "cancelled": 0, "unfinished": 0,
        "last_run": None, "seconds": 0,
    }


def _streak(days: set[date], today: date) -> int:
    day = today if today in days else today - timedelta(days=1)
    n = 0
    while day in days:
        n += 1
        day -= timedelta(days=1)
    return n


def _runs(rows: list, today: date) -> dict[str, Any]:
    by_command: dict[str, dict[str, Any]] = {}
    by_origin = dict.fromkeys(_ORIGINS, 0)
    totals = dict.fromkeys(("ok", "failed", "cancelled", "unfinished"), 0)
    per_day: Counter[date] = Counter()
    total_seconds = 0
    longest: dict[str, Any] | None = None

    for row in rows:
        command = str(row["command"])
        started = _int(row["started_at"])
        finished = row["finished_at"]
        status = row["status"]
        outcome = status if finished is not None and status in _RUN_STATUSES else "unfinished"

        entry = by_command.setdefault(command, _empty_command())
        entry["total"] += 1
        entry[outcome] += 1
        totals[outcome] += 1
        if entry["last_run"] is None or started > entry["last_run"]:
            entry["last_run"] = started
        if row["origin"] in by_origin:
            by_origin[row["origin"]] += 1
        per_day[datetime.fromtimestamp(started).date()] += 1

        if finished is not None:
            seconds = max(0, int(finished) - started)
            entry["seconds"] += seconds
            total_seconds += seconds
            if longest is None or seconds > longest["seconds"]:
                longest = {"command": command, "seconds": seconds, "started_at": started}

    busiest = None
    if per_day:
        day, n = max(per_day.items(), key=lambda kv: (kv[1], kv[0]))
        busiest = {"date": day.isoformat(), "runs": n}

    return {
        "total": len(rows),
        **totals,
        "by_origin": by_origin,
        "by_command": [
            {"command": command, **entry}
            for command, entry in sorted(
                by_command.items(), key=lambda kv: (-kv[1]["total"], kv[0])
            )
        ],
        "total_seconds": total_seconds,
        "longest": longest,
        "busiest_day": busiest,
        "active_days": len(per_day),
        "streak_days": _streak(set(per_day), today),
    }


def _per_space(stats: dict) -> list[dict]:
    """The per-space dicts of a ``{space: {...}}`` result, or the result itself."""
    if any(not isinstance(v, dict) for v in stats.values()):
        return [stats]
    return [v for v in stats.values() if isinstance(v, dict)]


def _work_extract(stats: dict, work: dict) -> None:
    for part in _per_space(stats):
        for key, field in (
            ("photos_processed", "photos_scanned"),
            ("faces_found", "faces_found"),
            ("restored", "faces_restored"),
        ):
            n = _num(part.get(key))
            if n is not None:
                work[field] += n
        detectors = part.get("detector_counts")
        if isinstance(detectors, dict):
            for name, value in detectors.items():
                n = _num(value)
                if n is not None:
                    work["detector_counts"][str(name)] = (
                        work["detector_counts"].get(str(name), 0) + n
                    )


def _work_sync(stats: dict, work: dict) -> None:
    for key, part in stats.items():
        if not isinstance(part, dict) or not isinstance(key, str):
            continue
        if key.endswith(".items"):
            n = _num(part.get("seen"))
            if n is None:
                n = _num(part.get("upserted"))
            if n is not None:
                work["photos_synced"] += n
        elif key.endswith(".hashes"):
            n = _num(part.get("hashed"))
            if n is not None:
                work["photos_hashed"] += n


def _work_regen(stats: dict, work: dict) -> None:
    for part in _per_space(stats):
        n = _num(part.get("crops"))
        if n is not None:
            work["crops_regenerated"] += n


def _work_flat(key: str, field: str):
    def extract(stats: dict, work: dict) -> None:
        n = _num(stats.get(key))
        if n is not None:
            work[field] += n

    return extract


_WORK_EXTRACTORS = {
    "extract": _work_extract,
    "sync": _work_sync,
    "regen-crops": _work_regen,
    "dedupe": _work_flat("groups", "duplicate_groups_found"),
    "prune-queue": _work_flat("pruned", "queue_items_pruned"),
}


def _work(rows: list) -> dict[str, Any]:
    work: dict[str, Any] = {
        "photos_scanned": 0, "faces_found": 0, "faces_restored": 0,
        "detector_counts": {}, "photos_synced": 0, "photos_hashed": 0,
        "crops_regenerated": 0, "duplicate_groups_found": 0, "queue_items_pruned": 0,
    }
    for row in rows:
        extractor = _WORK_EXTRACTORS.get(row["command"])
        raw = row["stats_json"]
        if extractor is None or not raw:
            continue
        try:
            stats = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(stats, dict):
            extractor(stats, work)
    for key, value in work.items():
        if key == "detector_counts":
            work[key] = {name: int(n) for name, n in value.items()}
        else:
            work[key] = int(value)
    return work


def _is_write(action: str) -> bool:
    """A NAS write: not a rehearsal, not a read-back check, not a local backup."""
    return not (action.startswith(("dryrun.", "backup.")) or action.endswith(".verify"))


def _nas(conn: Connection) -> tuple[dict[str, Any], int | None]:
    rows = conn.execute(
        "SELECT action, success, COUNT(*) AS n, MIN(ts) AS first_ts "
        "FROM audit_log GROUP BY action, success"
    ).fetchall()
    out: dict[str, Any] = {
        "writes_ok": 0, "writes_failed": 0, "dry_runs": 0, "by_action": [],
        "faces_tagged": 0, "faces_moved": 0, "merges": 0, "people_named": 0,
        "duplicates_deleted": 0,
    }
    by_action: dict[str, dict[str, Any]] = {}
    first_write: int | None = None
    for row in rows:
        action, n = str(row["action"]), _int(row["n"])
        if action.startswith("dryrun."):
            out["dry_runs"] += n
            continue
        if not _is_write(action) or row["success"] is None:
            continue
        entry = by_action.setdefault(action, {"action": action, "ok": 0, "failed": 0})
        if int(row["success"]):
            entry["ok"] += n
            out["writes_ok"] += n
            ts = _int(row["first_ts"])
            first_write = ts if first_write is None else min(first_write, ts)
            if action.endswith(".assign.add_face"):
                out["faces_tagged"] += n
            elif action.endswith(".reassign.separate"):
                out["faces_moved"] += n
            elif action.endswith(".merge"):
                out["merges"] += n
            elif action.endswith(".rename"):
                out["people_named"] += n
        else:
            entry["failed"] += n
            out["writes_failed"] += n
    out["by_action"] = sorted(by_action.values(), key=lambda e: (-e["ok"], e["action"]))

    # One dedupe.delete row deletes a whole batch of photos.
    for row in conn.execute(
        "SELECT params_json FROM audit_log WHERE action = 'dedupe.delete' AND success = 1"
    ):
        try:
            ids = json.loads(row["params_json"] or "{}").get("item_id")
        except (TypeError, ValueError, AttributeError):
            ids = None
        out["duplicates_deleted"] += len(ids) if isinstance(ids, list) else 1
    return out, first_write


def _review(conn: Connection) -> dict[str, Any]:
    counts = {
        str(row["status"]): _int(row["n"])
        for row in conn.execute(
            "SELECT status, COUNT(*) AS n FROM review_queue "
            "WHERE decided_by IS NOT NULL GROUP BY status"
        )
    }
    approved = sum(counts.get(s, 0) for s in ("approved", "applied", "failed"))
    rejected = counts.get("rejected", 0)
    return {
        "decided": sum(counts.values()),
        "approved": approved,
        "rejected": rejected,
        "hidden": counts.get("hidden", 0),
        "approval_rate": (
            approved / (approved + rejected) if approved + rejected else None
        ),
    }


def lifetime(conn: Connection, *, today: date | None = None) -> dict[str, Any]:
    """What Synopticon has done, from the run ledger, the audit log and the queue."""
    rows = conn.execute(
        "SELECT command, origin, started_at, finished_at, status, stats_json "
        "FROM command_runs ORDER BY started_at, id"
    ).fetchall()
    nas, first_write = _nas(conn)
    return {
        "tracking_since": _int(rows[0]["started_at"]) if rows else None,
        "first_nas_write": first_write,
        "runs": _runs(rows, today or date.today()),
        "work": _work(rows),
        "nas": nas,
        "review": _review(conn),
    }


# --------------------------------------------------------------------------- #
# Library
# --------------------------------------------------------------------------- #
def _photo_fact(row) -> dict[str, Any] | None:
    if row is None:
        return None
    return {"time": int(row["time"]), "inspect": inspect_path(row["space"], row["id"])}


def _taken_histograms(conn: Connection) -> dict[str, Any]:
    """Year / hour / weekday / busiest-day buckets of live photos' taken time."""
    hours: Counter[int] = Counter()
    for row in conn.execute(
        "SELECT p.time / 3600 AS h, COUNT(*) AS n FROM photos p "
        "WHERE p.deleted = 0 AND p.time >= 0 GROUP BY p.time / 3600"
    ):
        hours[int(row["h"])] += int(row["n"])
    # SQL integer division truncates toward zero; floor the rare pre-1970 photo here.
    for row in conn.execute("SELECT p.time FROM photos p WHERE p.deleted = 0 AND p.time < 0"):
        hours[int(row["time"]) // 3600] += 1

    by_hour = [0] * 24
    days: Counter[int] = Counter()
    for h, n in hours.items():
        if not _MIN_DAY <= h // 24 <= _MAX_DAY:
            continue
        by_hour[h % 24] += n
        days[h // 24] += n

    by_weekday = [0] * 7
    years: Counter[int] = Counter()
    for d, n in days.items():
        by_weekday[(d + 3) % 7] += n  # 1970-01-01 was a Thursday
        years[(_EPOCH + timedelta(days=d)).year] += n

    per_year = (
        [{"year": y, "count": years.get(y, 0)} for y in range(min(years), max(years) + 1)]
        if years else []
    )
    busiest = None
    if days:
        d, n = max(days.items(), key=lambda kv: (kv[1], -kv[0]))
        busiest = {"date": (_EPOCH + timedelta(days=d)).isoformat(), "count": n}
    return {
        "per_year": per_year, "by_hour": by_hour, "by_weekday": by_weekday,
        "busiest_day": busiest,
    }


def _growth(conn: Connection) -> list[dict[str, Any]]:
    months: Counter[tuple[int, int]] = Counter()
    for row in conn.execute(
        "SELECT p.indexed_time / 86400000 AS d, COUNT(*) AS n FROM photos p "
        "WHERE p.deleted = 0 AND p.indexed_time >= 0 GROUP BY p.indexed_time / 86400000"
    ):
        day = _EPOCH + timedelta(days=int(row["d"]))
        months[(day.year, day.month)] += int(row["n"])
    if not months:
        return []
    out = []
    total = 0
    year, month = min(months)
    last = max(months)
    while (year, month) <= last:
        total += months.get((year, month), 0)
        out.append({"month": f"{year:04d}-{month:02d}", "total": total})
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


def _photos(conn: Connection, settings: Settings) -> dict[str, Any]:
    by_space = dict.fromkeys(settings.nas.spaces, 0)
    by_type = dict.fromkeys((*_PHOTO_TYPES, "other"), 0)
    total = deleted = size = 0
    for row in conn.execute(
        "SELECT space, deleted, type, COUNT(*) AS n, COALESCE(SUM(filesize), 0) AS bytes "
        "FROM photos GROUP BY space, deleted, type"
    ):
        n = int(row["n"])
        if int(row["deleted"]):
            deleted += n
            continue
        total += n
        size += int(row["bytes"])
        by_space[row["space"]] = by_space.get(row["space"], 0) + n
        kind = row["type"] if row["type"] in _PHOTO_TYPES else "other"
        by_type[kind] += n

    oldest = conn.execute(
        "SELECT space, id, time FROM photos "
        "WHERE deleted = 0 AND time IS NOT NULL ORDER BY time, space, id LIMIT 1"
    ).fetchone()
    newest = conn.execute(
        "SELECT space, id, time FROM photos "
        "WHERE deleted = 0 AND time IS NOT NULL ORDER BY time DESC, space, id LIMIT 1"
    ).fetchone()
    duplicates = sum(
        int(row["n"]) - int(row["distinct_n"])
        for row in conn.execute(
            "SELECT space, COUNT(*) AS n, COUNT(DISTINCT sha256) AS distinct_n FROM photos "
            "WHERE deleted = 0 AND sha256 IS NOT NULL GROUP BY space"
        )
    )
    return {
        "total": total,
        "deleted": deleted,
        "by_space": by_space,
        "by_type": by_type,
        "bytes": size,
        "oldest": _photo_fact(oldest),
        "newest": _photo_fact(newest),
        **_taken_histograms(conn),
        "growth": _growth(conn),
        "duplicate_copies": duplicates,
    }


def _per_photo_bucket(n: int) -> str:
    if n < 5:
        return str(max(n, 0))
    return "5–9" if n < 10 else "10+"


def _face_counts(conn: Connection) -> tuple[dict[str, int], int, int]:
    """``(per detector, restored, embeddings)`` of faces not on a deleted photo.

    Counted as all faces minus those on deleted photos, a sliver reached through
    ``idx_photos_deleted``; filtering every face instead costs a probe per face.
    """
    detectors: Counter[str] = Counter()
    restored = 0
    select = (
        "SELECT f.detector, COUNT(*) AS n, "
        "COALESCE(SUM(CASE WHEN f.restored = 1 THEN 1 ELSE 0 END), 0) AS restored "
    )
    for sign, source in (
        (1, "FROM faces f GROUP BY f.detector"),
        (-1, (
            "FROM photos p JOIN faces f ON f.space = p.space AND f.photo_id = p.id "
            "WHERE p.deleted = 1 GROUP BY f.detector"
        )),
    ):
        for r in conn.execute(select + source):
            detectors[str(r["detector"])] += sign * int(r["n"])
            restored += sign * int(r["restored"])
    embeddings = int(conn.execute("SELECT COUNT(*) AS n FROM embeddings").fetchone()["n"]) - int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM photos p "
            "JOIN faces f ON f.space = p.space AND f.photo_id = p.id "
            "JOIN embeddings e ON e.face_id = f.face_id WHERE p.deleted = 1"
        ).fetchone()["n"]
    )
    return {k: n for k, n in detectors.items() if n}, restored, embeddings


def _faces(conn: Connection) -> dict[str, Any]:
    detectors, restored, embeddings = _face_counts(conn)
    detected = sum(detectors.values())

    buckets = dict.fromkeys(_PER_PHOTO_BUCKETS, 0)
    scanned = faces_total = 0
    for r in conn.execute(
        "SELECT e.face_count, COUNT(*) AS n FROM extract_log e "
        "JOIN photos p ON p.space = e.space AND p.id = e.photo_id "
        "WHERE p.deleted = 0 GROUP BY e.face_count"
    ):
        count, n = int(r["face_count"]), int(r["n"])
        buckets[_per_photo_bucket(count)] += n
        scanned += n
        faces_total += count * n

    crowded = conn.execute(
        "SELECT e.space, e.photo_id, e.face_count FROM extract_log e "
        "JOIN photos p ON p.space = e.space AND p.id = e.photo_id "
        "WHERE p.deleted = 0 AND e.face_count > 0 "
        "ORDER BY e.face_count DESC, e.space, e.photo_id LIMIT 1"
    ).fetchone()
    tiniest = conn.execute(
        f"SELECT f.space, f.photo_id, f.w, f.h FROM faces f WHERE {_FACE_LIVE} "
        "ORDER BY f.w * f.h, f.face_id LIMIT 1"
    ).fetchone()
    sharpest = conn.execute(
        f"SELECT f.space, f.photo_id, f.det_score FROM faces f "
        f"WHERE f.det_score IS NOT NULL AND {_FACE_LIVE} "
        "ORDER BY f.det_score DESC, f.face_id LIMIT 1"
    ).fetchone()

    return {
        "detected": detected,
        "embeddings": embeddings,
        "restored": restored,
        "detector_counts": detectors,
        "per_photo": [{"bucket": b, "photos": n} for b, n in buckets.items()],
        "photos_scanned": scanned,
        "photos_without_faces": buckets["0"],
        "avg_per_photo": faces_total / scanned if scanned else None,
        "most_crowded": None if crowded is None else {
            "faces": int(crowded["face_count"]),
            "inspect": inspect_path(crowded["space"], crowded["photo_id"]),
        },
        "tiniest": None if tiniest is None else {
            "width": round(float(tiniest["w"])),
            "height": round(float(tiniest["h"])),
            "inspect": inspect_path(tiniest["space"], tiniest["photo_id"]),
        },
        "sharpest_eye": None if sharpest is None else {
            "det_score": float(sharpest["det_score"]),
            "inspect": inspect_path(sharpest["space"], sharpest["photo_id"]),
        },
    }


def _gini(values: list[int]) -> float | None:
    total = sum(values)
    n = len(values)
    if not n or not total:
        return None
    ranked = sum(i * v for i, v in enumerate(sorted(values), start=1))
    return 2 * ranked / (n * total) - (n + 1) / n


def _people(conn: Connection, settings: Settings) -> dict[str, Any]:
    named: dict[tuple[str, int], str] = {}
    hidden = 0
    for row in conn.execute("SELECT space, id, name, show, deleted FROM persons"):
        if int(row["deleted"] or 0):
            continue
        if row["show"] is not None and int(row["show"]) == 0:
            hidden += 1
            continue
        name = (row["name"] or "").strip()
        if row["show"] is not None and int(row["show"]) == 1 and name:
            named[(row["space"], int(row["id"]))] = name

    tagged = 0
    counts: dict[tuple[str, int], int] = {}
    for row in conn.execute(
        "SELECT sf.space, sf.person_id, COUNT(*) AS n FROM syno_faces sf "
        "JOIN photos p ON p.space = sf.space AND p.id = sf.photo_id "
        "WHERE p.deleted = 0 AND sf.person_id IS NOT NULL GROUP BY sf.space, sf.person_id"
    ):
        n = int(row["n"])
        tagged += n
        key = (row["space"], int(row["person_id"]))
        if key in named:
            counts[key] = n

    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], named[kv[0]], kv[0]))
    named_faces = sum(counts.values())
    base = syno_web_base(settings)

    def entry(key: tuple[str, int], n: int) -> dict[str, Any]:
        space, person_id = key
        return {
            "name": named[key], "space": space, "person_id": person_id,
            "faces": n, "share": _share(n, tagged),
            "person_url": person_url(base, space, person_id),
        }

    top = ranked[:_DISTRIBUTION_TOP]
    rest = ranked[_DISTRIBUTION_TOP:]
    rest_faces = sum(n for _, n in rest)
    main = None
    if ranked:
        first = entry(*ranked[0])
        main = {k: first[k] for k in ("name", "faces", "share", "person_url")}
    return {
        "listed_named": len(named),
        "hidden": hidden,
        "synology_tagged": tagged,
        "synology_named": named_faces,
        "unnamed_or_unlisted_faces": tagged - named_faces,
        "one_face_people": sum(1 for n in counts.values() if n == 1),
        "distribution": [entry(k, n) for k, n in top],
        "everyone_else": {
            "people": len(rest), "faces": rest_faces, "share": _share(rest_faces, tagged),
        },
        "unlisted_share": _share(tagged - named_faces, tagged),
        "top10_share": _share(sum(n for _, n in ranked[:10]), tagged),
        "gini": _gini(list(counts.values())),
        "main_character": main,
    }


def _grouping(conn: Connection) -> dict[str, Any] | None:
    run = conn.execute(
        "SELECT run_id, created_at FROM cluster_runs ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    if run is None:
        return None
    row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(MAX(size), 0) AS largest, "
        "COALESCE(SUM(CASE WHEN size = 1 THEN 1 ELSE 0 END), 0) AS singletons, "
        "COALESCE(SUM(CASE WHEN mapped_person_id IS NOT NULL THEN 1 ELSE 0 END), 0) AS mapped "
        "FROM clusters WHERE run_id = ?",
        (run["run_id"],),
    ).fetchone()
    clusters = int(row["n"])
    return {
        "run_id": int(run["run_id"]),
        "created_at": int(run["created_at"]),
        "clusters": clusters,
        "largest": int(row["largest"]),
        "singletons": int(row["singletons"]),
        "mapped": int(row["mapped"]),
        "unmapped": clusters - int(row["mapped"]),
    }


def library(conn: Connection, settings: Settings) -> dict[str, Any]:
    """What the library is. O(library): serve it through :class:`LibraryCache`."""
    people = _people(conn, settings)
    faces = {
        "synology_tagged": people.pop("synology_tagged"),
        "synology_named": people.pop("synology_named"),
        **_faces(conn),
    }
    return {
        "computed_at": store.now(),
        "photos": _photos(conn, settings),
        "faces": faces,
        "people": people,
        "grouping": _grouping(conn),
    }


#: Counts/extents over every table :func:`library` reads. ``photos.synced_at``
#: moves on every sync and ``hashed_at`` on every hash pass, which covers the
#: columns updated in place (type, time, filesize, sha256); the name-length sum
#: catches a rename that changes no count.
_LIBRARY_FINGERPRINT_SQL = """
SELECT
  (SELECT COUNT(*) FROM photos),
  (SELECT MAX(id) FROM photos),
  (SELECT COALESCE(SUM(deleted), 0) FROM photos),
  (SELECT MAX(synced_at) FROM photos),
  (SELECT MAX(hashed_at) FROM photos),
  (SELECT COUNT(*) FROM faces),
  (SELECT MAX(face_id) FROM faces),
  (SELECT COUNT(*) FROM embeddings),
  (SELECT COUNT(*) FROM syno_faces),
  (SELECT MAX(syno_face_id) FROM syno_faces),
  (SELECT COALESCE(SUM(person_id), 0) FROM syno_faces),
  (SELECT COUNT(*) FROM persons),
  (SELECT COALESCE(SUM(show), 0) FROM persons),
  (SELECT COALESCE(SUM(deleted), 0) FROM persons),
  (SELECT COALESCE(SUM(LENGTH(name)), 0) FROM persons),
  (SELECT COUNT(*) FROM extract_log),
  (SELECT MAX(processed_at) FROM extract_log),
  (SELECT MAX(run_id) FROM cluster_runs)
"""


def library_fingerprint(conn: Connection) -> tuple:
    try:
        return tuple(conn.execute(_LIBRARY_FINGERPRINT_SQL).fetchone())
    except db_errors.DatabaseError:
        conn.rollback()
        return (None, object())  # never equal to a previous key


class LibraryCache:
    """Fingerprint-keyed cache of :func:`library`, safe across threads.

    The rebuild runs under the lock so concurrent requests after an
    invalidation share one rebuild, as :class:`~synopticon.review.lookups.LookupCache` does.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._key: Any = None
        self._value: dict[str, Any] | None = None

    def get(self, conn: Connection, settings: Settings) -> dict[str, Any]:
        key = library_fingerprint(conn)
        with self._lock:
            if self._value is not None and key == self._key:
                return self._value
            value = library(conn, settings)
            self._key, self._value = key, value
            return value

    def invalidate(self) -> None:
        with self._lock:
            self._key, self._value = None, None
