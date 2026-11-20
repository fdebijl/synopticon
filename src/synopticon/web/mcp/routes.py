"""Human-only REST routes that manage MCP tokens.

No ``from __future__ import annotations`` here: FastAPI would turn the
``Request`` parameters into required query fields (422 on every call).
"""


from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from ...config import Settings
from .. import auth
from .registry import SCOPES, normalize_scopes

ENDPOINT = "/mcp"


def register_mcp_routes(app: FastAPI, settings: Settings, conn) -> None:
    def _human_only(request: Request) -> JSONResponse | None:
        ident = getattr(request.state, "ident", None)
        if not ident or ident[0] != "user":
            return JSONResponse({"error": "must be signed in"}, status_code=403)
        return None

    @app.get("/api/mcp/status")
    async def api_mcp_status(request: Request):
        denied = _human_only(request)
        if denied is not None:
            return denied
        return {
            "enabled": bool(settings.mcp.enabled),
            "endpoint": ENDPOINT,
            "scopes": [
                {
                    "id": s.id,
                    "label": s.label,
                    "description": s.description,
                    "danger": s.danger,
                    "requires": list(s.requires),
                }
                for s in SCOPES
            ],
        }

    @app.get("/api/mcp/tokens")
    async def api_mcp_list_tokens(request: Request):
        denied = _human_only(request)
        if denied is not None:
            return denied

        def work():
            c = conn()
            try:
                return auth.list_mcp_tokens(c)
            finally:
                c.close()

        return {"tokens": await run_in_threadpool(work)}

    @app.post("/api/mcp/tokens")
    async def api_mcp_create_token(request: Request):
        denied = _human_only(request)
        if denied is not None:
            return denied
        body = await request.json()
        if not isinstance(body, dict):
            return JSONResponse({"error": "body must be an object"}, status_code=422)
        name = body.get("name")
        name = name.strip() if isinstance(name, str) else ""
        if not name:
            return JSONResponse({"error": "name is required"}, status_code=422)
        scopes = body.get("scopes", [])
        if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
            return JSONResponse({"error": "scopes must be a list of strings"}, status_code=422)

        def work():
            c = conn()
            try:
                return auth.create_mcp_token(c, name, scopes)
            finally:
                c.close()

        try:
            token = await run_in_threadpool(work)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        return JSONResponse({"token": token, "name": name, "scopes": normalize_scopes(scopes)}, status_code=201)

    @app.post("/api/mcp/tokens/{token_id}/revoke")
    async def api_mcp_revoke_token(token_id: int, request: Request):
        denied = _human_only(request)
        if denied is not None:
            return denied

        def work():
            c = conn()
            try:
                return auth.revoke_mcp_token(c, token_id)
            finally:
                c.close()

        if not await run_in_threadpool(work):
            return JSONResponse({"error": "no such token"}, status_code=404)
        return {"ok": True}
