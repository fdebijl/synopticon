"""The contract between the MCP protocol layer and the tools it serves.

A tool is a plain synchronous function ``handler(ctx, args) -> dict | ToolOutput``.
The protocol layer runs it in the AnyIO threadpool (ADR 07), so a handler may
open connections, read files and do O(library) work freely, but it must never
import ``pipeline.runner`` or anything that loads cv2/onnxruntime.

Scopes are checked twice: a tool is only *listed* to a token holding at least
one of ``Tool.scopes``, and a handler whose required scope depends on its
arguments (``run_job``) narrows that with ``ctx.require``.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any, Callable

from ...config import Settings
from ...db import Connection

# --------------------------------------------------------------------------- #
# Scopes                                                                        #
# --------------------------------------------------------------------------- #

READ = "read"
CONFIG = "config"
JOBS = "jobs"
REVIEW = "review"
MAINTENANCE = "maintenance"
APPLY = "apply"
APPLY_REASSIGN = "apply:reassign"
APPLY_MERGE = "apply:merge"


@dataclass(frozen=True)
class ScopeInfo:
    id: str
    label: str
    description: str
    #: "safe": reads only. "local": changes Synopticon's own data or config.
    #: "nas": writes to the photo library on the NAS.
    danger: str
    #: Scopes that must also be granted for this one to be valid.
    requires: tuple[str, ...] = ()


#: Every scope a token can carry, in the order the UI lists them. ``read`` is
#: implied on every token. No scope reaches a typed-phrase gate (merging two
#: named people, deleting duplicates, ``reset --all``): the MCP always submits
#: with ``confirm_phrase=None``, exactly like a schedule.
SCOPES: tuple[ScopeInfo, ...] = (
    ScopeInfo(
        READ,
        "Read",
        "Status, settings, job logs, the review queue, and face detection and "
        "grouping diagnostics, including face crops and photo previews. Always granted.",
        "safe",
    ),
    ScopeInfo(
        CONFIG,
        "Tune settings",
        "Change the Inference, Face detection, Restoration, Face grouping and "
        "Crossref settings. Never NAS, Storage, Database, Security or MCP settings.",
        "local",
    ),
    ScopeInfo(
        JOBS,
        "Run pipeline steps",
        "Start and cancel Sync, Detect faces, Group faces, reports, crop "
        "regeneration, benchmarks and model downloads, plus dry-run previews of "
        "Apply and Dedupe. None of these write to the NAS.",
        "local",
    ),
    ScopeInfo(
        REVIEW,
        "Decide review items",
        "Approve, reject, hide or reset suggestions in the review queue. Nothing "
        "reaches the NAS until an Apply runs.",
        "local",
    ),
    ScopeInfo(
        MAINTENANCE,
        "Maintenance",
        "Clear the review queue or apply history, prune orphaned suggestions, "
        "delete crops, and reset derived data (never a full reset).",
        "local",
    ),
    ScopeInfo(
        APPLY,
        "Apply to NAS",
        "Write approved assign, low-confidence and new-person suggestions to "
        "Synology Photos.",
        "nas",
    ),
    ScopeInfo(
        APPLY_REASSIGN,
        "Apply reassigns",
        "Also move faces Synology already tagged to a different person.",
        "nas",
        requires=(APPLY,),
    ),
    ScopeInfo(
        APPLY_MERGE,
        "Apply merges",
        "Also merge an unnamed person into another. Merging two named people is "
        "never available to an agent.",
        "nas",
        requires=(APPLY,),
    ),
)

SCOPE_IDS: frozenset[str] = frozenset(s.id for s in SCOPES)


def normalize_scopes(requested: list[str] | tuple[str, ...] | set[str]) -> list[str]:
    """Validate a requested scope set and return it in catalogue order.

    ``read`` is always added. Raises ``ValueError`` naming the first unknown
    scope or unmet requirement.
    """
    wanted = set(requested or ()) | {READ}
    unknown = sorted(wanted - SCOPE_IDS)
    if unknown:
        raise ValueError(f"unknown scope: {unknown[0]!r}")
    for info in SCOPES:
        if info.id in wanted:
            missing = [r for r in info.requires if r not in wanted]
            if missing:
                raise ValueError(f"scope {info.id!r} also needs {missing[0]!r}")
    return [s.id for s in SCOPES if s.id in wanted]


# --------------------------------------------------------------------------- #
# Tool plumbing                                                                 #
# --------------------------------------------------------------------------- #


class ToolError(Exception):
    """A tool call that failed in a way the agent should read and act on.

    Reported as a successful JSON-RPC response with ``isError: true`` (the MCP
    convention for tool-level failures), never as a protocol error.
    """


class ScopeError(ToolError):
    def __init__(self, scope: str, why: str = ""):
        self.scope = scope
        msg = f"this token lacks the {scope!r} scope"
        if why:
            msg += f" ({why})"
        msg += ". A human can mint a token with it under Settings -> Access -> MCP tokens."
        super().__init__(msg)


@dataclass(frozen=True)
class McpToken:
    id: int
    name: str
    scopes: frozenset[str]


@dataclass
class ToolContext:
    """Everything a handler may touch. Built per call by the protocol layer."""

    settings: Settings
    conn: Callable[[], Connection]
    token: McpToken
    #: The web process's running ``JobManager``.
    job_manager: Any
    #: ``review.lookups.VoidedCache`` shared with the review routes (ADR 18).
    voided: Any
    #: ``quickmerger.NasSession`` shared with Inspect (ADR 16); ``None`` in tests.
    nas_session: Any = None

    def has(self, scope: str) -> bool:
        return scope in self.token.scopes

    def require(self, scope: str, why: str = "") -> None:
        if scope not in self.token.scopes:
            raise ScopeError(scope, why)

    @property
    def actor(self) -> str:
        """What ``decided_by`` / log lines record for this token."""
        return f"mcp:{self.token.name}"


@dataclass
class ImageContent:
    data: bytes
    mime_type: str = "image/jpeg"
    #: Optional caption emitted as a text block just before the image.
    caption: str | None = None

    def to_mcp(self) -> dict:
        return {
            "type": "image",
            "data": base64.b64encode(self.data).decode("ascii"),
            "mimeType": self.mime_type,
        }


@dataclass
class ToolOutput:
    """A result with images. A handler that returns a plain dict gets that dict
    as both ``structuredContent`` and a JSON text block."""

    structured: dict | None = None
    images: list[ImageContent] = field(default_factory=list)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict
    #: Listed to a token holding ANY of these.
    scopes: frozenset[str]
    handler: Callable[[ToolContext, dict], "dict | ToolOutput"]
    #: MCP ``annotations``: readOnlyHint / destructiveHint / idempotentHint.
    read_only: bool = True
    destructive: bool = False

    def listing(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": {
                "readOnlyHint": self.read_only,
                "destructiveHint": self.destructive,
            },
        }


def tool(
    name: str,
    description: str,
    *,
    properties: dict | None = None,
    required: list[str] | None = None,
    scopes: tuple[str, ...] = (READ,),
    read_only: bool = True,
    destructive: bool = False,
) -> Callable[[Callable], Tool]:
    """Decorator building a :class:`Tool` from a handler function."""

    def wrap(fn: Callable) -> Tool:
        schema: dict = {
            "type": "object",
            "properties": properties or {},
            "additionalProperties": False,
        }
        if required:
            schema["required"] = list(required)
        return Tool(
            name=name,
            description=description,
            input_schema=schema,
            scopes=frozenset(scopes),
            handler=fn,
            read_only=read_only,
            destructive=destructive,
        )

    return wrap


# -- argument helpers shared by every tool module ---------------------------- #


def arg_int(
    args: dict, key: str, default: int | None = None, *, lo: int | None = None, hi: int | None = None
) -> int | None:
    value = args.get(key, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
        raise ToolError(f"{key!r} must be an integer")
    value = int(value)
    if lo is not None and value < lo:
        raise ToolError(f"{key!r} must be at least {lo}")
    if hi is not None and value > hi:
        raise ToolError(f"{key!r} must be at most {hi}")
    return value


def arg_str(
    args: dict, key: str, default: str | None = None, *, choices: tuple[str, ...] | None = None
) -> str | None:
    value = args.get(key, default)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ToolError(f"{key!r} must be a string")
    if choices is not None and value not in choices:
        raise ToolError(f"{key!r} must be one of {', '.join(choices)}")
    return value


def arg_bool(args: dict, key: str, default: bool = False) -> bool:
    value = args.get(key, default)
    if not isinstance(value, bool):
        raise ToolError(f"{key!r} must be true or false")
    return value
