"""Write-back to Synology Photos: add_face / merge / rename / reassign / delete_face.

Two `PersonWriter` implementations:
- `DryRunWriter` — audits every operation as if it happened, touches no network.
- `SynoWriter` — the real thing, HAR-verified `add_face` payload shape,
  optional `Upload.Face` crop upload, `list_face` verification.

`apply_reviewed` walks `review_queue` rows with `status='approved'`, re-checks
NAS state immediately before each write (idempotent skip), and stops early
after `stop_after_failures` consecutive failures (circuit breaker).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol

from synopticon import audit
from synopticon.config import Space
from synopticon.db import Connection, store
from synopticon.progress import EventLogHandler, get_emitter
from synopticon.syno import foto
from synopticon.syno.client import QuotedString, SynoApiError, SynoClient
from synopticon.syno.models import WriteResult

# Dedicated logger for the write-back / apply path. The CLI attaches a file
# handler via `configure_apply_logging`; on its own this logger is inert
# (no handler, no propagation) so importing the module has no side effects.
log = logging.getLogger("synopticon.apply")

# review_queue kinds that carry a single face->person assignment payload
# (photo_id / person_id / bbox_normalized) and are written via `writer.assign`.
# `low_confidence` items are cluster-crossref suggestions the reviewer approved
# one at a time; structurally they are just assigns.
ASSIGN_KINDS = frozenset({"assign", "low_confidence"})

# review_queue kind that moves an existing Synology face to a different person
# via `Browse.Person.separate` (HAR-verified: the face keeps its face_id and is
# re-bound to the target person). Gated behind `apply_reassigns`.
REASSIGN_KIND = "reassign"

# Merge kinds, split by danger. `merge` joins a cluster where at least one side
# is unnamed (the routine case); `merge_named` joins two *already-named* people —
# irreversible and destroys a human-assigned label, so it carries a separate,
# stricter apply gate (`apply_merges_named`). Both write via `Browse.Person.merge`
# and are otherwise handled identically.
MERGE_KIND = "merge"
MERGE_NAMED_KIND = "merge_named"
MERGE_KINDS = frozenset({MERGE_KIND, MERGE_NAMED_KIND})


def configure_apply_logging(logfile: str | Path, level: int = logging.INFO) -> Path:
    """Route the `synopticon.apply` logger to `logfile` (idempotent).

    Adds a single `FileHandler` for `logfile`; calling again with the same path
    is a no-op, so repeated `apply` invocations in one process don't stack
    handlers. Returns the resolved log path.
    """
    logfile = Path(logfile)
    resolved = logfile.expanduser().resolve()

    # Bridge apply-logger records into the structured progress stream as `log`
    # events (idempotent, like the file handler below). This covers both this
    # module and dedupe_writeback, which share the `synopticon.apply` logger.
    if not any(isinstance(h, EventLogHandler) for h in log.handlers):
        log.addHandler(EventLogHandler())

    for handler in log.handlers:
        if isinstance(handler, logging.FileHandler) and Path(handler.baseFilename) == resolved:
            log.setLevel(level)
            return resolved
    resolved.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(resolved, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(level)
    log.propagate = False  # keep apply chatter out of any root/console config
    return resolved


class PersonWriter(Protocol):
    def assign(
        self,
        item_id: int,
        person_id: int,
        bbox_normalized: tuple[float, float, float, float],
        face_crop_jpeg: bytes | None = None,
    ) -> WriteResult: ...

    def merge(self, target_id: int, merged_ids: list[int], name: str) -> WriteResult: ...

    def rename(self, person_id: int, name: str) -> WriteResult: ...

    def set_show(self, person_id: int, show: bool) -> WriteResult: ...

    def delete_face(self, space: Space, face_id: int, person_id: int) -> WriteResult: ...

    def reassign(
        self,
        space: Space,
        syno_face_id: int,
        target_person_id: int,
        target_name: str,
        item_id: int,
    ) -> WriteResult: ...


class DryRunWriter:
    """Audit-only rehearsal: every call becomes a `dryrun.*` audit_log row, no NAS I/O."""

    dry_run = True

    def __init__(self, conn: Connection):
        self.conn = conn
        self.client = None
        self.space: Space | None = None

    def _record(self, action: str, params: dict) -> WriteResult:
        log.info("dryrun %s %s", action, params)
        audit.record(
            self.conn, action=f"dryrun.{action}", api=None, params=params, response=None, success=True
        )
        return WriteResult(success=True, api="dryrun", method=action, request_params=params, response=None)

    def assign(self, item_id, person_id, bbox_normalized, face_crop_jpeg=None) -> WriteResult:
        return self._record(
            "assign",
            {
                "item_id": item_id,
                "person_id": person_id,
                "bbox": list(bbox_normalized),
                "has_crop": face_crop_jpeg is not None,
            },
        )

    def merge(self, target_id, merged_ids, name) -> WriteResult:
        return self._record("merge", {"target_id": target_id, "merged_ids": merged_ids, "name": name})

    def rename(self, person_id, name) -> WriteResult:
        return self._record("rename", {"person_id": person_id, "name": name})

    def set_show(self, person_id, show) -> WriteResult:
        return self._record("set_show", {"person_id": person_id, "show": bool(show)})

    def delete_face(self, space, face_id, person_id) -> WriteResult:
        return self._record(
            "delete_face", {"space": space, "face_id": face_id, "person_id": person_id}
        )

    def reassign(
        self, space, syno_face_id, target_person_id, target_name, item_id
    ) -> WriteResult:
        return self._record(
            "reassign",
            {
                "space": space,
                "syno_face_id": syno_face_id,
                "target_person_id": target_person_id,
                "target_name": target_name,
                "item_id": item_id,
            },
        )


class SynoWriter:
    """Real writer: talks to the NAS via `SynoClient`, audits every step.

    `action_prefix` names the caller in `audit_log.action` — the review-queue
    apply path leaves it at `writeback`, interactive tools (QuickMerger) pass
    their own so the audit trail says which surface issued the write.
    """

    dry_run = False

    def __init__(
        self,
        client: SynoClient,
        conn: Connection,
        space: Space,
        action_prefix: str = "writeback",
    ):
        self.client = client
        self.conn = conn
        self.space = space
        self.action_prefix = action_prefix

    def _action(self, suffix: str) -> str:
        return f"{self.action_prefix}.{suffix}"

    def _simple_call(self, api: str, method: str, version: int, params: dict, action: str) -> WriteResult:
        log.info("%s -> %s.%s v%s params=%s", action, api, method, version, params)
        try:
            data = self.client.call(api, method, version=version, **params)
            success, error_code = True, None
        except SynoApiError as exc:
            data, success, error_code = None, False, exc.code
        if success:
            log.info("%s ok", action)
        else:
            log.warning("%s FAILED code=%s", action, error_code)
        audit.record(
            self.conn,
            action=action,
            api=f"{api}.{method}",
            params={**params, "version": version},
            response=data,
            success=success,
        )
        return WriteResult(
            success=success, api=api, method=method, request_params=params, response=data, error_code=error_code
        )

    def assign(
        self,
        item_id: int,
        person_id: int,
        bbox_normalized: tuple[float, float, float, float],
        face_crop_jpeg: bytes | None = None,
    ) -> WriteResult:
        space = self.space
        x1, y1, x2, y2 = bbox_normalized
        face_id_temp = f"{item_id}-0"
        face_entry = {
            "face_bounding_box": {"top_left": {"x": x1, "y": y1}, "bottom_right": {"x": x2, "y": y2}},
            "face_id_temp": face_id_temp,
            "person_id": person_id,
        }
        api = self.client.api_name(space, "Browse.Person")
        version = self.client.version_for(api, 3)
        params = {"face": [face_entry], "id_item": item_id}
        log.info(
            "assign -> item=%s person=%s space=%s bbox=%s has_crop=%s",
            item_id, person_id, space, bbox_normalized, face_crop_jpeg is not None,
        )

        try:
            data = self.client.call(api, "add_face", version=version, **params)
            add_success, error_code = True, None
        except SynoApiError as exc:
            data, add_success, error_code = None, False, exc.code

        audit.record(
            self.conn,
            action=self._action("assign.add_face"),
            api=f"{api}.add_face",
            params={**params, "version": version},
            response=data,
            success=add_success,
        )
        if not add_success:
            log.warning("assign add_face FAILED item=%s person=%s code=%s", item_id, person_id, error_code)
            return WriteResult(
                success=False, api=api, method="add_face", request_params=params, response=data,
                error_code=error_code,
            )

        real_face_id = None
        entries = (data or {}).get("list") or []
        for entry in entries:
            if entry.get("face_id_temp") == face_id_temp:
                real_face_id = entry.get("face_id")
                break
        if real_face_id is None and entries:
            real_face_id = entries[0].get("face_id")
        log.info("assign add_face ok item=%s person=%s face_id=%s", item_id, person_id, real_face_id)

        upload_success = True
        if face_crop_jpeg is not None and real_face_id is not None:
            upload_api = self.client.api_name(space, "Upload.Face")
            try:
                upload_data = self.client.upload(
                    upload_api,
                    method="upload",
                    version=1,
                    fields={"face_id": real_face_id},
                    file=("blob", face_crop_jpeg, "image/jpeg"),
                )
                upload_success = True
            except SynoApiError:
                upload_data, upload_success = None, False
            log.info(
                "assign upload_face %s item=%s face_id=%s",
                "ok" if upload_success else "FAILED", item_id, real_face_id,
            )
            audit.record(
                self.conn,
                action=self._action("assign.upload_face"),
                api=f"{upload_api}.upload",
                params={"face_id": real_face_id},
                response=upload_data,
                success=upload_success,
            )

        verify_success = False
        if real_face_id is not None:
            try:
                faces = foto.list_item_faces(self.client, space, item_id)
                verify_success = any(
                    f.face_id == real_face_id and f.person_id == person_id for f in faces
                )
            except SynoApiError:
                verify_success = False
        audit.record(
            self.conn,
            action=self._action("assign.verify"),
            api=f"{api}.list_face",
            params={"id_item": item_id, "face_id": real_face_id},
            response=None,
            success=verify_success,
        )

        log.info("assign verify %s item=%s face_id=%s", "ok" if verify_success else "FAILED", item_id, real_face_id)

        overall = add_success and upload_success and verify_success
        log.info(
            "assign done item=%s person=%s overall=%s (add=%s upload=%s verify=%s)",
            item_id, person_id, overall, add_success, upload_success, verify_success,
        )
        return WriteResult(
            success=overall, api=api, method="add_face", request_params=params, response=data, error_code=None
        )

    def merge(self, target_id: int, merged_ids: list[int], name: str) -> WriteResult:
        api = self.client.api_name(self.space, "Browse.Person")
        version = self.client.version_for(api, 2)
        params = {"target_id": target_id, "merged_id": merged_ids, "name": QuotedString(name)}
        return self._simple_call(api, "merge", version, params, self._action("merge"))

    def rename(self, person_id: int, name: str) -> WriteResult:
        api = self.client.api_name(self.space, "Browse.Person")
        version = self.client.version_for(api, 1)
        params = {"id": person_id, "name": QuotedString(name)}
        return self._simple_call(api, "set", version, params, self._action("rename"))

    def set_show(self, person_id: int, show: bool) -> WriteResult:
        """Hide/unhide a person in Synology Photos (`Browse.Person.show`).

        Reversible: the person and its faces are untouched, only the People
        view's visibility flag changes.
        """
        api = self.client.api_name(self.space, "Browse.Person")
        version = self.client.version_for(api, 1)
        params = {"id": [person_id], "show": bool(show)}
        return self._simple_call(api, "show", version, params, self._action("set_show"))

    def delete_face(self, space: Space, face_id: int, person_id: int) -> WriteResult:
        """Remove a face detection from a photo.

        WARNING: HAR-verified (har/remove_face_from_photo.har) to hard-delete
        the face record — a subsequent `list_face` returns nothing for it.
        This is NOT a reversible unassign; use `reassign` to move a face.
        """
        api = self.client.api_name(space, "Browse.Person")
        version = self.client.version_for(api, 1)
        params = {"face_id": [face_id], "person_id": person_id}
        return self._simple_call(api, "delete_face", version, params, self._action("delete_face"))

    def reassign(
        self,
        space: Space,
        syno_face_id: int,
        target_person_id: int,
        target_name: str,
        item_id: int,
    ) -> WriteResult:
        """Move an existing Synology face to a different person.

        Single atomic `Browse.Person.separate` call, byte-exact from captured
        traffic (har/reassign_existing_face_to_other_person.har): the face
        keeps its face_id and is re-bound to `target_person_id`. Verified via
        `list_face` afterwards. Reversible (another separate moves it back).
        """
        api = self.client.api_name(space, "Browse.Person")
        version = self.client.version_for(api, 1)
        params = {
            "face_id": [syno_face_id],
            "target_id": target_person_id,
            "name": QuotedString(target_name),
        }
        result = self._simple_call(api, "separate", version, params, self._action("reassign.separate"))
        if not result.success:
            return result

        verify_success = False
        try:
            faces = foto.list_item_faces(self.client, space, item_id)
            verify_success = any(
                f.face_id == syno_face_id and f.person_id == target_person_id
                for f in faces
            )
        except SynoApiError:
            verify_success = False
        audit.record(
            self.conn,
            action=self._action("reassign.verify"),
            api=f"{api}.list_face",
            params={"id_item": item_id, "face_id": syno_face_id},
            response=None,
            success=verify_success,
        )
        log.info(
            "reassign verify %s item=%s face_id=%s person=%s",
            "ok" if verify_success else "FAILED", item_id, syno_face_id, target_person_id,
        )
        return WriteResult(
            success=result.success and verify_success,
            api=api,
            method="separate",
            request_params=params,
            response=result.response,
            error_code=result.error_code,
        )


@dataclass
class ApplyStats:
    considered: int = 0
    applied: int = 0
    skipped: int = 0
    failed: int = 0


def _mark(conn: Connection, review_item_id: int, status: str) -> None:
    conn.execute(
        "UPDATE review_queue SET status = ?, decided_at = ? WHERE item_id = ?",
        (status, store.now(), review_item_id),
    )
    conn.commit()


def _person_name(conn: Connection, space: str, person_id: int) -> str | None:
    row = conn.execute(
        "SELECT name FROM persons WHERE space = ? AND id = ?", (space, person_id)
    ).fetchone()
    return row["name"] if row else None


def _merge_order(conn: Connection, person_a: dict, person_b: dict) -> tuple[dict, dict]:
    """Pick the merge target: prefer a named person, then the larger item_count."""

    def rank(p: dict) -> tuple[int, int]:
        row = conn.execute(
            "SELECT name, item_count FROM persons WHERE space = ? AND id = ?",
            (p.get("space", "personal"), p["person_id"]),
        ).fetchone()
        name = (row["name"] if row else None) or p.get("name") or ""
        count = (row["item_count"] if row else None) or 0
        return (1 if name.strip() else 0, count)

    return (person_a, person_b) if rank(person_a) >= rank(person_b) else (person_b, person_a)


def apply_reviewed(
    conn: Connection,
    writer: PersonWriter,
    kinds: Iterable[str],
    person_id: int | None = None,
    apply_merges: bool = False,
    apply_merges_named: bool = False,
    apply_reassigns: bool = False,
    stop_after_failures: int = 5,
) -> ApplyStats:
    """Apply approved review_queue rows via `writer`, idempotently, with a circuit breaker.

    `person_id`, when given, restricts to rows whose payload references that
    person (either side of a merge or a reassign). `kind='merge'` rows are
    skipped entirely unless `apply_merges=True`; the more dangerous
    `kind='merge_named'` rows (joining two already-named people) require the
    separate `apply_merges_named=True`. Likewise `kind='reassign'` rows require
    `apply_reassigns=True` (they alter existing human-visible labels). A
    NAS-backed `writer` (one exposing `.client`) gets a pre-write
    idempotency check; a client-less writer (e.g. DryRunWriter) always
    proceeds straight to the write call. A reassign whose Synology face has
    vanished from the NAS since the last sync is skipped (logged as drift),
    not written or marked applied.
    """
    kinds = list(kinds)
    stats = ApplyStats()
    if not kinds:
        return stats

    placeholders = ",".join("?" for _ in kinds)
    rows = conn.execute(
        f"SELECT * FROM review_queue WHERE status = 'approved' AND kind IN ({placeholders}) "
        "ORDER BY item_id",
        kinds,
    ).fetchall()

    client = getattr(writer, "client", None)
    writer_space = getattr(writer, "space", None)
    dry_run = getattr(writer, "dry_run", False)
    consecutive_failures = 0
    emitter = get_emitter()

    log.info(
        "apply_reviewed start: %s approved row(s) kinds=%s person_id=%s "
        "apply_merges=%s apply_merges_named=%s apply_reassigns=%s dry_run=%s",
        len(rows), kinds, person_id,
        apply_merges, apply_merges_named, apply_reassigns, dry_run,
    )

    def mark(item_id: int, status: str) -> None:
        # A dry run rehearses stats and audit rows but must never mutate
        # review_queue state — otherwise it consumes the pending approvals it
        # was meant to preview.
        if not dry_run:
            _mark(conn, item_id, status)

    for idx, row in enumerate(rows):
        emitter.progress("apply", idx + 1, len(rows))
        payload = json.loads(row["payload_json"])
        kind = row["kind"]

        if person_id is not None:
            if kind in ASSIGN_KINDS:
                if payload.get("person_id") != person_id:
                    continue
            elif kind in MERGE_KINDS:
                pa = (payload.get("person_a") or {}).get("person_id")
                pb = (payload.get("person_b") or {}).get("person_id")
                if person_id not in (pa, pb):
                    continue
            elif kind == REASSIGN_KIND:
                if person_id not in (payload.get("person_id"), payload.get("from_person_id")):
                    continue
            else:
                continue

        stats.considered += 1

        if kind == MERGE_KIND and not apply_merges:
            stats.skipped += 1
            continue

        if kind == MERGE_NAMED_KIND and not apply_merges_named:
            stats.skipped += 1
            continue

        if kind == REASSIGN_KIND and not apply_reassigns:
            stats.skipped += 1
            continue

        applied_already = False
        reassign_face_gone = False
        if client is not None:
            try:
                if kind in ASSIGN_KINDS:
                    space = payload.get("space", writer_space)
                    faces = foto.list_item_faces(client, space, payload["photo_id"])
                    applied_already = any(f.person_id == payload["person_id"] for f in faces)
                elif kind == REASSIGN_KIND:
                    space = payload.get("space", writer_space)
                    faces = foto.list_item_faces(client, space, payload["photo_id"])
                    target = next(
                        (f for f in faces if f.face_id == payload["syno_face_id"]), None
                    )
                    if target is None:
                        # The Synology face vanished since the last sync
                        # (deleted or merged away on the NAS): nothing to move.
                        reassign_face_gone = True
                    else:
                        applied_already = target.person_id == payload["person_id"]
                elif kind in MERGE_KINDS:
                    # The merged-away side is chosen by _merge_order, not always
                    # person_b — probe that side for absence to detect re-applies.
                    _, merged = _merge_order(conn, payload["person_a"], payload["person_b"])
                    try:
                        foto.get_person(client, merged["space"], merged["person_id"])
                    except (SynoApiError, LookupError):
                        applied_already = True
            except SynoApiError:
                applied_already = False

        if reassign_face_gone:
            log.warning(
                "item=%s kind=%s syno_face_id=%s no longer on photo %s (NAS drift) -> skip",
                row["item_id"], kind, payload.get("syno_face_id"), payload.get("photo_id"),
            )
            stats.skipped += 1
            continue

        if applied_already:
            log.info("item=%s kind=%s already applied on NAS -> skip write", row["item_id"], kind)
            mark(row["item_id"], "applied")
            stats.applied += 1
            consecutive_failures = 0
            continue

        if kind in ASSIGN_KINDS:
            result = writer.assign(
                payload["photo_id"],
                payload["person_id"],
                tuple(payload["bbox_normalized"]),
                payload.get("face_crop_jpeg"),
            )
        elif kind in MERGE_KINDS:
            target, merged = _merge_order(conn, payload["person_a"], payload["person_b"])
            name = target.get("name") or merged.get("name") or ""
            result = writer.merge(target["person_id"], [merged["person_id"]], name)
        elif kind == REASSIGN_KIND:
            space = payload.get("space", writer_space)
            # `separate` requires the target person's name; resolve it fresh
            # from the local mirror in case it was renamed since clustering.
            name = (
                _person_name(conn, space, payload["person_id"])
                or payload.get("person_name")
                or ""
            )
            result = writer.reassign(
                space,
                payload["syno_face_id"],
                payload["person_id"],
                name,
                payload["photo_id"],
            )
        else:
            log.info("item=%s kind=%s not writable -> skip", row["item_id"], kind)
            stats.skipped += 1
            continue

        if result.success:
            mark(row["item_id"], "applied")
            stats.applied += 1
            consecutive_failures = 0
        else:
            log.warning("item=%s kind=%s write failed code=%s", row["item_id"], kind, result.error_code)
            mark(row["item_id"], "failed")
            stats.failed += 1
            consecutive_failures += 1
            if consecutive_failures >= stop_after_failures:
                log.warning("circuit breaker: %s consecutive failures, stopping early", consecutive_failures)
                break

    log.info(
        "apply_reviewed done: considered=%s applied=%s skipped=%s failed=%s%s",
        stats.considered, stats.applied, stats.skipped, stats.failed,
        " (dry-run)" if dry_run else "",
    )
    emitter.result(
        ok=stats.failed == 0,
        stats={
            "considered": stats.considered,
            "applied": stats.applied,
            "skipped": stats.skipped,
            "failed": stats.failed,
            "dry_run": dry_run,
        },
    )
    return stats
