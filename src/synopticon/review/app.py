"""FastAPI + lightweight JS review UI (``[review]`` extra).

fastapi/uvicorn are imported lazily so the package imports without them.
The UI ONLY mutates ``review_queue`` — applying decisions is a separate CLI
command owned by another agent.

The data layer (item shaping + mutations) lives in ``review/queries.py`` so the
web GUI can share it; this module is a thin FastAPI wrapper over it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ..config import Settings
from ..db import store
from . import queries

_TEMPLATE_DIR = Path(__file__).parent / "templates"


def _require_fastapi():
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError as exc:  # pragma: no cover - trivial guard
        raise ImportError(
            "The review UI needs the [review] extra: pip install 'synopticon[review]'"
        ) from exc


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

    # persons/faces are static while the review server runs, so cache the two
    # ground-truth lookups for the app's lifetime (queries.py rebuilds on each
    # call; caching is the caller's job).
    _hidden: set[tuple[str, int]] | None = None
    _person_faces: dict[tuple[str, int], list[int]] | None = None

    def _cached_lookups(c: sqlite3.Connection):
        nonlocal _hidden, _person_faces
        if _hidden is None:
            _hidden = queries.hidden_persons(c)
        if _person_faces is None:
            _person_faces = queries.person_faces(c, settings)
        return _hidden, _person_faces

    @app.get("/", response_class=HTMLResponse)
    def index(kind: str = "", status: str = "pending"):
        c = conn()
        try:
            hidden, person_face_map = _cached_lookups(c)
            items = queries.load_review_items(
                c,
                settings,
                kind=kind,
                status=status,
                limit=500,
                offset=0,
                hidden=hidden,
                person_face_map=person_face_map,
            )
            template = env.get_template("app.html.j2")
            return template.render(items=items, kind=kind, status=status)
        finally:
            c.close()

    @app.post("/decide/{item_id}")
    def decide(item_id: int, decision: str = Form(...)):
        c = conn()
        try:
            status = queries.decide_item(c, item_id, decision)
        finally:
            c.close()
        if status is None:
            return JSONResponse({"error": "bad decision"}, status_code=400)
        return {"item_id": item_id, "status": status}

    @app.post("/bulk")
    def bulk(kind: str = Form(...), min_confidence: float = Form(0.0)):
        c = conn()
        try:
            approved = queries.bulk_approve(c, kind, min_confidence)
            return {"approved": approved}
        finally:
            c.close()

    @app.post("/name/{item_id}")
    def set_name(item_id: int, suggested_name: str = Form(...)):
        c = conn()
        try:
            ok = queries.set_suggested_name(c, item_id, suggested_name)
        finally:
            c.close()
        if not ok:
            return JSONResponse({"error": "not a new_person item"}, status_code=400)
        return {"item_id": item_id, "suggested_name": suggested_name}

    return app


def serve(settings: Settings, host: str = "127.0.0.1", port: int = 8686) -> None:
    """Run the review UI server. Requires the [review] extra."""
    _require_fastapi()
    import uvicorn

    app = create_app(settings.storage.db_path, settings)
    uvicorn.run(app, host=host, port=port)
