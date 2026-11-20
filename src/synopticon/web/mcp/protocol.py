"""The ``/mcp`` endpoint: stateless JSON-RPC 2.0 over POST, as a pure ASGI app.

No SSE, no sessions, no dependencies. It authenticates itself with an MCP token
(``web_mcp_tokens``) and never accepts a cookie or a REST API key. Pure ASGI,
never ``BaseHTTPMiddleware`` (ADR 07 invariant 4); every database, file and
subprocess call a tool makes runs in the threadpool.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable
from urllib.parse import urlsplit

from starlette.concurrency import run_in_threadpool

from ...config import Settings
from ...db import Connection, store
from .. import auth, clientip
from .registry import (
    SCOPES,
    McpToken,
    Tool,
    ToolContext,
    ToolError,
    ToolOutput,
)

log = logging.getLogger("synopticon.mcp")

SUPPORTED_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
MAX_BODY_BYTES = 1024 * 1024

DISABLED_MESSAGE = (
    "The MCP endpoint is turned off. Turn on Settings -> MCP -> Enabled, then "
    "restart Synopticon."
)

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602


def all_tools() -> list[Tool]:
    from . import tools_core, tools_diag

    return [*tools_core.TOOLS, *tools_diag.TOOLS]


def _instructions(token: McpToken) -> str:
    scopes = ", ".join(s.id for s in SCOPES if s.id in token.scopes)
    return (
        "Synopticon supplements Synology Photos' face recognition. It works in five "
        "phases: Sync (copy the library's metadata from the NAS), Detect faces "
        "(job 'extract'), Group faces (job 'cluster' / 'recluster'), Review "
        "(suggestions in a queue a person or you decide on) and Apply (the only phase "
        "that writes to the NAS). Everything before Apply is read-only toward the NAS.\n\n"
        "Tuning loop: get_overview, then get_config, then update_config, then run_job "
        "'recluster' (grouping settings) or 'extract' (detection settings, which "
        "re-detects photos), poll get_job until it finishes, then use the grouping and "
        "detection diagnostic tools to see whether it helped. get_face_crops shows "
        "faces.\n\n"
        f"This token ({token.name!r}) holds the scopes: {scopes}. A tool outside them "
        "is not listed. Apply defaults to a dry-run preview. Operations that need a "
        "typed confirmation phrase (merging two named people, deleting duplicates, a "
        "full reset) are human-only and cannot be performed through this endpoint."
    )


def _rpc_result(msg_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _rpc_error(msg_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _text(value: Any) -> dict:
    return {"type": "text", "text": value if isinstance(value, str) else json.dumps(value, default=str)}


def _error_result(message: str) -> dict:
    return {"content": [_text(message)], "isError": True}


def _success_result(output: "dict | ToolOutput") -> dict:
    if isinstance(output, ToolOutput):
        content: list[dict] = []
        if output.structured is not None:
            content.append(_text(output.structured))
        for image in output.images:
            if image.caption:
                content.append(_text(image.caption))
            content.append(image.to_mcp())
        result: dict = {"content": content}
        if output.structured is not None:
            result["structuredContent"] = output.structured
        return result
    return {"content": [_text(output)], "structuredContent": output}


def make_mcp_app(
    settings: Settings,
    *,
    job_manager: Any,
    voided: Any,
    nas_session: Callable[[], Any] | None = None,
    server_version: str = "dev",
):
    def conn() -> Connection:
        return store.connect(settings)

    async def respond(send, status: int, body: Any, headers: list[tuple[bytes, bytes]] | None = None):
        payload = b"" if body is None else json.dumps(body, default=str).encode("utf-8")
        head = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode()),
            (b"cache-control", b"no-store"),
            *(headers or []),
        ]
        await send({"type": "http.response.start", "status": status, "headers": head})
        await send({"type": "http.response.body", "body": payload})

    async def read_body(scope, receive) -> bytes | None:
        """The request body, or ``None`` when it exceeds the cap."""
        for key, value in scope.get("headers", ()):
            if key == b"content-length":
                try:
                    if int(value) > MAX_BODY_BYTES:
                        return None
                except ValueError:
                    pass
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return b""
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > MAX_BODY_BYTES:
                return None
            chunks.append(chunk)
            if not message.get("more_body", False):
                return b"".join(chunks)

    def header(scope, name: bytes) -> str | None:
        for key, value in scope.get("headers", ()):
            if key == name:
                return value.decode("latin-1")
        return None

    def origin_mismatch(scope) -> bool:
        origin = header(scope, b"origin")
        if origin is None:
            return False
        host = (header(scope, b"host") or "").strip().lower()
        try:
            netloc = urlsplit(origin).netloc.lower()
        except ValueError:
            return True
        return not netloc or netloc != host

    def authenticate(scope) -> McpToken | None:
        raw = header(scope, b"authorization") or ""
        presented = raw[7:].strip() if raw[:7].lower() == "bearer " else ""
        c = conn()
        try:
            token = auth.validate_mcp_token(c, presented) if presented else None
            if token is None and settings.security.log_failed_api_keys:
                ip = clientip.client_ip(scope)
                if auth.authlog.blocked_log.allow(f"mcp:{clientip.ip_prefix(ip)}"):
                    auth.record_attempt(
                        c,
                        event="mcp_token",
                        outcome="failure",
                        reason="unknown_or_revoked_token" if presented else "missing_token",
                        ip=ip,
                        user_agent=clientip.user_agent(scope),
                    )
            return token
        finally:
            c.close()

    def visible(token: McpToken, tools: list[Tool]) -> list[Tool]:
        return [t for t in tools if t.scopes & token.scopes]

    def run_tool(tool: Tool, token: McpToken, arguments: dict) -> dict:
        ctx = ToolContext(
            settings=settings,
            conn=conn,
            token=token,
            job_manager=job_manager,
            voided=voided,
            nas_session=nas_session() if nas_session is not None else None,
        )
        try:
            return _success_result(tool.handler(ctx, arguments))
        except ToolError as exc:
            return _error_result(str(exc))
        except Exception as exc:  # noqa: BLE001 - never leak a traceback to the agent
            log.exception("mcp tool %s failed (token %r)", tool.name, token.name)
            return _error_result(f"internal error: {type(exc).__name__}")

    async def dispatch(message: dict, token: McpToken) -> dict | None:
        method = message.get("method")
        msg_id = message.get("id")
        if not isinstance(method, str):
            if "result" in message or "error" in message:
                return None
            return _rpc_error(msg_id, INVALID_REQUEST, "missing method")
        if "id" not in message:
            return None
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return _rpc_error(msg_id, INVALID_PARAMS, "params must be an object")

        if method == "initialize":
            requested = params.get("protocolVersion")
            version = requested if requested in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
            return _rpc_result(
                msg_id,
                {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "synopticon", "version": server_version},
                    "instructions": _instructions(token),
                },
            )
        if method == "ping":
            return _rpc_result(msg_id, {})
        if method == "tools/list":
            tools = visible(token, all_tools())
            return _rpc_result(msg_id, {"tools": [t.listing() for t in tools]})
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments")
            if arguments is None:
                arguments = {}
            if not isinstance(name, str) or not isinstance(arguments, dict):
                return _rpc_error(msg_id, INVALID_PARAMS, "name (string) and arguments (object) are required")
            tool = next((t for t in visible(token, all_tools()) if t.name == name), None)
            if tool is None:
                return _rpc_error(msg_id, INVALID_PARAMS, f"unknown tool: {name!r}")
            log.info("mcp tools/call %s by token %r", name, token.name)
            return _rpc_result(msg_id, await run_in_threadpool(run_tool, tool, token, arguments))
        return _rpc_error(msg_id, METHOD_NOT_FOUND, f"method not found: {method}")

    async def app(scope, receive, send):
        if scope["type"] != "http":
            return

        if not settings.mcp.enabled:
            await respond(send, 404, {"error": DISABLED_MESSAGE})
            return

        if origin_mismatch(scope):
            await respond(send, 403, {"error": "Origin does not match Host"})
            return

        if scope["method"] != "POST":
            await respond(
                send, 405, {"error": "Only POST is supported"}, [(b"allow", b"POST")]
            )
            return

        token = await run_in_threadpool(authenticate, scope)
        if token is None:
            await respond(
                send,
                401,
                {"error": "A valid MCP token is required (Authorization: Bearer synm_...)."},
                [(b"www-authenticate", b"Bearer")],
            )
            return

        body = await read_body(scope, receive)
        if body is None:
            await respond(send, 413, {"error": "request body too large"})
            return
        try:
            message = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            await respond(send, 400, _rpc_error(None, PARSE_ERROR, "parse error"))
            return
        if isinstance(message, list):
            await respond(
                send, 400, _rpc_error(None, INVALID_REQUEST, "batch requests are not supported")
            )
            return
        if not isinstance(message, dict):
            await respond(send, 400, _rpc_error(None, INVALID_REQUEST, "invalid request"))
            return

        reply = await dispatch(message, token)
        if reply is None:
            await respond(send, 202, None)
        else:
            await respond(send, 200, reply)

    return app
