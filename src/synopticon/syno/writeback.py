"""Write-back to Synology Photos: add_face / merge / rename / delete_face.

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
import sqlite3
from dataclasses import dataclass
from typing import Iterable, Protocol

from synopticon import audit
from synopticon.config import Space
from synopticon.db import store
from synopticon.syno import foto
from synopticon.syno.client import QuotedString, SynoApiError, SynoClient
from synopticon.syno.models import WriteResult


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

    def delete_face(self, space: Space, face_id: int) -> WriteResult: ...


class DryRunWriter:
    """Audit-only rehearsal: every call becomes a `dryrun.*` audit_log row, no NAS I/O."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.client = None
        self.space: Space | None = None

    def _record(self, action: str, params: dict) -> WriteResult:
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

    def delete_face(self, space, face_id) -> WriteResult:
        return self._record("delete_face", {"space": space, "face_id": face_id})


class SynoWriter:
    """Real writer: talks to the NAS via `SynoClient`, audits every step."""

    def __init__(self, client: SynoClient, conn: sqlite3.Connection, space: Space):
        self.client = client
        self.conn = conn
        self.space = space

    def _simple_call(self, api: str, method: str, version: int, params: dict, action: str) -> WriteResult:
        try:
            data = self.client.call(api, method, version=version, **params)
            success, error_code = True, None
        except SynoApiError as exc:
            data, success, error_code = None, False, exc.code
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

        try:
            data = self.client.call(api, "add_face", version=version, **params)
            add_success, error_code = True, None
        except SynoApiError as exc:
            data, add_success, error_code = None, False, exc.code

        audit.record(
            self.conn,
            action="writeback.assign.add_face",
            api=f"{api}.add_face",
            params={**params, "version": version},
            response=data,
            success=add_success,
        )
        if not add_success:
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
            audit.record(
                self.conn,
                action="writeback.assign.upload_face",
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
            action="writeback.assign.verify",
            api=f"{api}.list_face",
            params={"id_item": item_id, "face_id": real_face_id},
            response=None,
            success=verify_success,
        )

        overall = add_success and upload_success and verify_success
        return WriteResult(
            success=overall, api=api, method="add_face", request_params=params, response=data, error_code=None
        )

    def merge(self, target_id: int, merged_ids: list[int], name: str) -> WriteResult:
        api = self.client.api_name(self.space, "Browse.Person")
        version = self.client.version_for(api, 2)
        params = {"target_id": target_id, "merged_id": merged_ids, "name": QuotedString(name)}
        return self._simple_call(api, "merge", version, params, "writeback.merge")

    def rename(self, person_id: int, name: str) -> WriteResult:
        api = self.client.api_name(self.space, "Browse.Person")
        version = self.client.version_for(api, 1)
        params = {"id": person_id, "name": QuotedString(name)}
        return self._simple_call(api, "set", version, params, "writeback.rename")

    def delete_face(self, space: Space, face_id: int) -> WriteResult:
        api = self.client.api_name(space, "Browse.Person")
        version = self.client.version_for(api, 1)
        params = {"face_id": [face_id]}
        return self._simple_call(api, "delete_face", version, params, "writeback.delete_face")


@dataclass
class ApplyStats:
    considered: int = 0
    applied: int = 0
    skipped: int = 0
    failed: int = 0


def _mark(conn: sqlite3.Connection, review_item_id: int, status: str) -> None:
    conn.execute(
        "UPDATE review_queue SET status = ?, decided_at = ? WHERE item_id = ?",
        (status, store.now(), review_item_id),
    )
    conn.commit()


def _merge_order(conn: sqlite3.Connection, person_a: dict, person_b: dict) -> tuple[dict, dict]:
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
    conn: sqlite3.Connection,
    writer: PersonWriter,
    kinds: Iterable[str],
    person_id: int | None = None,
    apply_merges: bool = False,
    stop_after_failures: int = 5,
) -> ApplyStats:
    """Apply approved review_queue rows via `writer`, idempotently, with a circuit breaker.

    `person_id`, when given, restricts to rows whose payload references that
    person (either side of a merge). `kind='merge'` rows are skipped entirely
    unless `apply_merges=True`. A NAS-backed `writer` (one exposing `.client`)
    gets a pre-write idempotency check; a client-less writer (e.g. DryRunWriter)
    always proceeds straight to the write call.
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
    consecutive_failures = 0

    for row in rows:
        payload = json.loads(row["payload_json"])
        kind = row["kind"]

        if person_id is not None:
            if kind == "assign":
                if payload.get("person_id") != person_id:
                    continue
            elif kind == "merge":
                pa = (payload.get("person_a") or {}).get("person_id")
                pb = (payload.get("person_b") or {}).get("person_id")
                if person_id not in (pa, pb):
                    continue
            else:
                continue

        stats.considered += 1

        if kind == "merge" and not apply_merges:
            stats.skipped += 1
            continue

        applied_already = False
        if client is not None:
            try:
                if kind == "assign":
                    space = payload.get("space", writer_space)
                    faces = foto.list_item_faces(client, space, payload["photo_id"])
                    applied_already = any(f.person_id == payload["person_id"] for f in faces)
                elif kind == "merge":
                    person_b = payload["person_b"]
                    try:
                        foto.get_person(client, person_b["space"], person_b["person_id"])
                    except (SynoApiError, LookupError):
                        applied_already = True
            except SynoApiError:
                applied_already = False

        if applied_already:
            _mark(conn, row["item_id"], "applied")
            stats.applied += 1
            consecutive_failures = 0
            continue

        if kind == "assign":
            result = writer.assign(
                payload["photo_id"],
                payload["person_id"],
                tuple(payload["bbox_normalized"]),
                payload.get("face_crop_jpeg"),
            )
        elif kind == "merge":
            target, merged = _merge_order(conn, payload["person_a"], payload["person_b"])
            name = target.get("name") or merged.get("name") or ""
            result = writer.merge(target["person_id"], [merged["person_id"]], name)
        else:
            stats.skipped += 1
            continue

        if result.success:
            _mark(conn, row["item_id"], "applied")
            stats.applied += 1
            consecutive_failures = 0
        else:
            _mark(conn, row["item_id"], "failed")
            stats.failed += 1
            consecutive_failures += 1
            if consecutive_failures >= stop_after_failures:
                break

    return stats
