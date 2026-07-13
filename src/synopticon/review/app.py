"""FastAPI + lightweight JS review UI (``[review]`` extra).

fastapi/uvicorn are imported lazily so the package imports without them.
The UI ONLY mutates ``review_queue`` — applying decisions is a separate CLI
command owned by another agent.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from ..config import Settings
from ..db import store

_TEMPLATE_DIR = Path(__file__).parent / "templates"


def _require_fastapi():
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError as exc:  # pragma: no cover - trivial guard
        raise ImportError(
            "The review UI needs the [review] extra: pip install 'synopticon[review]'"
        ) from exc


def _crop_url(crop_path: str | None, crops_dir: Path) -> str | None:
    if not crop_path:
        return None
    # The stored crop_path may be absolute or CWD-relative, and when relative it
    # already includes the crops_dir prefix (the runner stores the full path).
    # Resolve both to absolute and take the path relative to crops_dir, which is
    # what the /crops static mount serves.
    try:
        rel = os.path.relpath(Path(crop_path).resolve(), crops_dir.resolve())
    except ValueError:  # e.g. different drive on Windows
        return None
    if rel.startswith(".."):  # outside the served crops dir
        return None
    return "/crops/" + rel.replace(os.sep, "/")


def create_app(db_path: Path | str, settings: Settings):
    """Build the FastAPI app. Requires the [review] extra."""
    _require_fastapi()
    from fastapi import FastAPI, Form
    from fastapi.responses import HTMLResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    db_path = Path(db_path)
    crops_dir = Path(settings.storage.crops_dir)
    crops_dir.mkdir(parents=True, exist_ok=True)

    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "j2"]),
    )

    app = FastAPI(title="Synopticon review")
    app.mount("/crops", StaticFiles(directory=str(crops_dir)), name="crops")

    def conn() -> sqlite3.Connection:
        return store.connect(db_path)

    def _face_crops(c: sqlite3.Connection) -> dict[int, str | None]:
        return {
            int(r["face_id"]): _crop_url(r["crop_path"], crops_dir)
            for r in c.execute("SELECT face_id, crop_path FROM faces")
        }

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

    @app.get("/", response_class=HTMLResponse)
    def index(kind: str = "", status: str = "pending"):
        c = conn()
        try:
            clauses, args = [], []
            if status:
                clauses.append("status = ?")
                args.append(status)
            if kind:
                clauses.append("kind = ?")
                args.append(kind)
            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            rows = c.execute(
                f"SELECT item_id, kind, payload_json, confidence, status "
                f"FROM review_queue {where} ORDER BY item_id LIMIT 500",
                args,
            ).fetchall()
            crops = _face_crops(c)
            items = []
            for r in rows:
                payload = json.loads(r["payload_json"])
                exemplars = (payload.get("evidence") or {}).get("exemplars", {})
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
                        "new_person_crops": [
                            crops.get(int(f)) for f in payload.get("face_ids", [])
                        ],
                        "merge_crops_a": _merge_side_crops(
                            payload.get("person_a"), exemplars, crops
                        ),
                        "merge_crops_b": _merge_side_crops(
                            payload.get("person_b"), exemplars, crops
                        ),
                    }
                )
            template = env.get_template("app.html.j2")
            return template.render(items=items, kind=kind, status=status)
        finally:
            c.close()

    @app.post("/decide/{item_id}")
    def decide(item_id: int, decision: str = Form(...)):
        status = {"approve": "approved", "reject": "rejected"}.get(decision)
        if status is None:
            return JSONResponse({"error": "bad decision"}, status_code=400)
        c = conn()
        try:
            c.execute(
                "UPDATE review_queue SET status = ?, decided_at = ?, decided_by = ? "
                "WHERE item_id = ?",
                (status, store.now(), "review-ui", item_id),
            )
            c.commit()
        finally:
            c.close()
        return {"item_id": item_id, "status": status}

    @app.post("/bulk")
    def bulk(kind: str = Form(...), min_confidence: float = Form(0.0)):
        c = conn()
        try:
            cur = c.execute(
                "UPDATE review_queue SET status = 'approved', decided_at = ?, "
                "decided_by = 'review-ui' WHERE kind = ? AND status = 'pending' "
                "AND confidence IS NOT NULL AND confidence >= ?",
                (store.now(), kind, min_confidence),
            )
            c.commit()
            return {"approved": cur.rowcount}
        finally:
            c.close()

    @app.post("/name/{item_id}")
    def set_name(item_id: int, suggested_name: str = Form(...)):
        c = conn()
        try:
            row = c.execute(
                "SELECT payload_json, kind FROM review_queue WHERE item_id = ?",
                (item_id,),
            ).fetchone()
            if row is None or row["kind"] != "new_person":
                return JSONResponse({"error": "not a new_person item"}, status_code=400)
            payload: dict[str, Any] = json.loads(row["payload_json"])
            payload["suggested_name"] = suggested_name
            c.execute(
                "UPDATE review_queue SET payload_json = ? WHERE item_id = ?",
                (json.dumps(payload), item_id),
            )
            c.commit()
            return {"item_id": item_id, "suggested_name": suggested_name}
        finally:
            c.close()

    return app


def serve(settings: Settings, host: str = "127.0.0.1", port: int = 8686) -> None:
    """Run the review UI server. Requires the [review] extra."""
    _require_fastapi()
    import uvicorn

    app = create_app(settings.storage.db_path, settings)
    uvicorn.run(app, host=host, port=port)
