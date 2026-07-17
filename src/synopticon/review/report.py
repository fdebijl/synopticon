"""Static self-contained HTML review report + decisions.jsonl.

No JavaScript, inline CSS only. Crop images are referenced by relative path
so the report directory can be copied/served as a static bundle.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..config import Settings

_TEMPLATE_DIR = Path(__file__).parent / "templates"
SECTION_CAP = 500


def _crop_rel(crop_path: str | None, report_dir: Path, crops_dir: Path) -> str | None:
    if not crop_path:
        return None
    p = Path(crop_path)
    if not p.is_absolute():
        p = crops_dir / p
    try:
        return os.path.relpath(p, report_dir)
    except ValueError:
        return str(p)


def _face_crops(conn: sqlite3.Connection) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for row in conn.execute("SELECT face_id, crop_path, ctx_crop_path FROM faces"):
        out[int(row["face_id"])] = {
            "crop_path": row["crop_path"],
            "ctx_crop_path": row["ctx_crop_path"],
        }
    return out


def generate(conn: sqlite3.Connection, settings: Settings, run_id: int) -> Path:
    """Render ``{reports_dir}/{run_id}/index.html`` and ``decisions.jsonl``."""
    report_dir = settings.storage.reports_dir / str(run_id)
    report_dir.mkdir(parents=True, exist_ok=True)
    crops_dir = settings.storage.crops_dir

    # Summary stats.
    faces = conn.execute(
        "SELECT COUNT(*) FROM cluster_members WHERE run_id = ?", (run_id,)
    ).fetchone()[0]
    n_clusters = conn.execute(
        "SELECT COUNT(*) FROM clusters WHERE run_id = ?", (run_id,)
    ).fetchone()[0]
    n_mapped = conn.execute(
        "SELECT COUNT(*) FROM clusters WHERE run_id = ? AND mapped_person_id IS NOT NULL",
        (run_id,),
    ).fetchone()[0]

    rows = conn.execute(
        "SELECT item_id, kind, payload_json, confidence, status FROM review_queue "
        "WHERE run_id = ? ORDER BY item_id",
        (run_id,),
    ).fetchall()

    face_crops = _face_crops(conn)

    def crop_for(fid) -> str | None:
        fc = face_crops.get(int(fid)) if fid is not None else None
        if not fc:
            return None
        return _crop_rel(fc["crop_path"], report_dir, crops_dir)

    counts_by_kind: dict[str, int] = {}
    sections: dict[str, list[dict]] = {
        "assign": [],
        "reassign": [],
        "merge": [],
        "new_person": [],
        "flags": [],
    }
    decisions: list[dict] = []

    for row in rows:
        kind = row["kind"]
        counts_by_kind[kind] = counts_by_kind.get(kind, 0) + 1
        payload = json.loads(row["payload_json"])
        decisions.append(
            {
                "item_id": row["item_id"],
                "kind": kind,
                "confidence": row["confidence"],
                "status": row["status"],
                "payload": payload,
            }
        )
        entry = {
            "item_id": row["item_id"],
            "kind": kind,
            "confidence": row["confidence"],
            "status": row["status"],
            "payload": payload,
        }
        if kind == "assign":
            entry["crop"] = crop_for(payload.get("face_id"))
            sections["assign"].append(entry)
        elif kind == "reassign":
            entry["crop"] = crop_for(payload.get("face_id"))
            sections["reassign"].append(entry)
        elif kind == "merge":
            ev = payload.get("evidence", {})
            entry["exemplars_a"] = [
                crop_for(f)
                for f in ev.get("exemplars", {}).get(
                    _pk(payload.get("person_a")), []
                )
            ]
            entry["exemplars_b"] = [
                crop_for(f)
                for f in ev.get("exemplars", {}).get(
                    _pk(payload.get("person_b")), []
                )
            ]
            sections["merge"].append(entry)
        elif kind == "new_person":
            entry["crops"] = [crop_for(f) for f in payload.get("face_ids", [])]
            sections["new_person"].append(entry)
        else:  # low_confidence / restore_disagreement -> flags
            entry["crop"] = crop_for(payload.get("face_id"))
            sections["flags"].append(entry)

    truncated = {k: max(0, len(v) - SECTION_CAP) for k, v in sections.items()}
    for k in sections:
        sections[k] = sections[k][:SECTION_CAP]

    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "j2"]),
    )
    template = env.get_template("report.html.j2")
    html = template.render(
        run_id=run_id,
        faces=faces,
        n_clusters=n_clusters,
        n_mapped=n_mapped,
        counts_by_kind=counts_by_kind,
        sections=sections,
        truncated=truncated,
    )

    index_path = report_dir / "index.html"
    index_path.write_text(html, encoding="utf-8")

    with (report_dir / "decisions.jsonl").open("w", encoding="utf-8") as fh:
        for d in decisions:
            fh.write(json.dumps(d) + "\n")

    return index_path


def _pk(person: dict | None) -> str:
    if not person:
        return ""
    return f"{person.get('space')}:{person.get('person_id')}"
