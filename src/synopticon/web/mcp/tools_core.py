"""Status, settings, jobs, review and crop tools served over MCP."""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, SecretStr, ValidationError

from ...config import Settings, load_settings
from ...db import errors as db_errors
from ...review import queries
from .. import configio
from ..jobs import (
    JOB_SPECS,
    ConsentError,
    JobParamError,
    QueueFullError,
    resolve_argv,
)
from ..stats import gather_stats
from .registry import (
    APPLY,
    APPLY_MERGE,
    APPLY_REASSIGN,
    CONFIG,
    JOBS,
    MAINTENANCE,
    REVIEW,
    ImageContent,
    ToolError,
    ToolOutput,
    arg_bool,
    arg_int,
    arg_str,
    tool,
)

log = logging.getLogger("synopticon.mcp")

#: Sections an agent may edit. `nas`, `storage`, `database`, `security` and
#: `mcp` decide where credentials go, who can reach the server and whether
#: this endpoint exists, so they are never writable here.
WRITABLE_SECTIONS = ("inference", "detection", "restoration", "clustering", "crossref")

MAINTENANCE_JOBS = frozenset(
    {"clear-queue", "clear-applies", "prune-queue", "delete-crops", "reset"}
)

_SUBPROCESS_TIMEOUT = 120
_OUTPUT_CAP = 20000
_EVENT_STRING_CAP = 2000
_ACTIVE_STATES = ("queued", "running")

_REVIEW_DECISIONS = ("approve", "reject", "hide", "pending")
_DECIDABLE = frozenset({"pending", "approved", "rejected", "hidden"})


def _package_version() -> str:
    try:
        from importlib.metadata import version

        return version("synopticon")
    except Exception:  # noqa: BLE001
        return "dev"


def _job_running(job_manager: Any) -> bool:
    return any(j.get("state") in _ACTIVE_STATES for j in job_manager.list_jobs())


def _clip(value: Any, cap: int = _EVENT_STRING_CAP) -> Any:
    if isinstance(value, str) and len(value) > cap:
        return value[:cap] + "..."
    return value


def _slim_job(meta: dict) -> dict:
    return {
        "id": meta.get("id"),
        "name": meta.get("name"),
        "state": meta.get("state"),
        "origin": meta.get("origin"),
        "created_at": meta.get("created_at"),
        "started_at": meta.get("started_at"),
        "ended_at": meta.get("ended_at"),
        "exit_code": meta.get("exit_code"),
        "error": meta.get("error"),
        "argv": meta.get("argv"),
        "progress": meta.get("progress"),
    }


# --------------------------------------------------------------------------- #
# Read tools                                                                    #
# --------------------------------------------------------------------------- #


@tool(
    "get_overview",
    "Start here. Returns the Synopticon version, this token's name and scopes, library "
    "statistics (photos, faces, detection coverage against the current pipeline_version, "
    "latest grouping run), review-queue counts (deleted photos excluded), the most "
    "recent run of every command, and any jobs currently queued or running. Scope: read.",
)
def get_overview(ctx, args):
    c = ctx.conn()
    try:
        stats = gather_stats(c, ctx.settings, voided=ctx.voided.get(c))
        last_runs = []
        for row in c.execute(
            "SELECT id, command, origin, job_id, started_at, finished_at, status "
            "FROM command_runs WHERE id IN (SELECT MAX(id) FROM command_runs GROUP BY command) "
            "ORDER BY command"
        ):
            last_runs.append({k: row[k] for k in row.keys()})
    finally:
        c.close()
    extract = stats["extract"]
    stale = None
    if extract["processed"] is not None:
        stale = max(0, extract["eligible"] - extract["processed"])
    return {
        "version": _package_version(),
        "token": {"name": ctx.token.name, "scopes": sorted(ctx.token.scopes)},
        "stats": stats,
        "review_counts": stats["review"],
        "pipeline": {
            "pipeline_version": extract["pipeline_version"],
            "models_ready": extract["models_ready"],
            "photos_needing_detection": stale,
        },
        "last_runs": last_runs,
        "active_jobs": [
            _slim_job(j) for j in ctx.job_manager.list_jobs() if j.get("state") in _ACTIVE_STATES
        ],
    }


def _section_schema(schema: dict, section: str) -> dict:
    prop = schema["properties"][section]
    ref = prop.get("$ref") or (prop.get("allOf") or [{}])[0].get("$ref")
    if not ref:
        return {}
    return schema.get("$defs", {}).get(ref.rsplit("/", 1)[-1], {}).get("properties", {})


def _field_type(spec: dict) -> Any:
    if "type" in spec:
        return spec["type"]
    options = spec.get("anyOf")
    if options:
        return [o.get("type") for o in options if "type" in o]
    return None


@tool(
    "get_config",
    "Read Synopticon's settings, freshly from disk (not the web process's start-up copy). "
    "Per field: current value, default, plain-language description, technical details, "
    "type, allowed values and bounds. Secrets are masked. Also lists env_overrides "
    "(settings shadowed by SYNOPTICON_* environment variables, which a config edit cannot "
    "change), writable_sections for this token, and which sections need re-detection. "
    "Pass 'section' (e.g. 'clustering') to read one section. Scope: read.",
    properties={"section": {"type": "string", "description": "One section name; omit for all."}},
)
def get_config(ctx, args):
    section = arg_str(args, "section")
    fresh = load_settings()
    values = configio._dump_masked(fresh)
    if section is not None and section not in values:
        raise ToolError(f"unknown section {section!r}; sections: {', '.join(sorted(values))}")
    schema = Settings.model_json_schema()
    out: dict[str, Any] = {}
    for name in [section] if section else list(values):
        fields = _section_schema(schema, name)
        out[name] = {
            key: {
                "value": value,
                "default": fields.get(key, {}).get("default"),
                "description": fields.get(key, {}).get("description"),
                "details": fields.get(key, {}).get("details"),
                "type": _field_type(fields.get(key, {})),
                "enum": fields.get(key, {}).get("enum"),
                "minimum": fields.get(key, {}).get("minimum"),
                "maximum": fields.get(key, {}).get("maximum"),
            }
            for key, value in values[name].items()
        }
    return {
        "config": out,
        "env_overrides": configio._env_overrides(fresh),
        "writable_sections": list(WRITABLE_SECTIONS) if ctx.has(CONFIG) else [],
        "notes": (
            "Changing [detection] (or swapping models) changes the pipeline_version, so every "
            "photo is re-detected on the next Detect faces run (job 'extract'). [clustering] "
            "and [crossref] changes only need job 'recluster'. [inference] and [restoration] "
            "do not change the pipeline_version."
        ),
    }


@tool(
    "list_jobs",
    "List recent jobs (newest first) with state, argv, origin and a progress snapshot. "
    "Use get_job for a job's log. Scope: read.",
    properties={"limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}},
)
def list_jobs(ctx, args):
    limit = arg_int(args, "limit", 20, lo=1, hi=100)
    return {"jobs": [_slim_job(j) for j in ctx.job_manager.history(limit)]}


@tool(
    "get_job",
    "One job's metadata, progress snapshot and log events. Without 'after' you get the "
    "last 'limit' events; with 'after' (a seq from a previous call's next_after) you get "
    "the following events, oldest first, so you can poll a running job cheaply. Events "
    "include log lines, progress, errors and the final result. Scope: read.",
    properties={
        "job_id": {"type": "string"},
        "after": {"type": "integer", "minimum": 0, "description": "Return events with seq greater than this."},
        "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
    },
    required=["job_id"],
)
def get_job(ctx, args):
    job_id = arg_str(args, "job_id")
    after = arg_int(args, "after", None, lo=0)
    limit = arg_int(args, "limit", 100, lo=1, hi=500)
    meta = ctx.job_manager.get(job_id)
    if meta is None:
        raise ToolError(f"unknown job {job_id!r}")
    events = ctx.job_manager.events(job_id, after=after or 0)
    events = events[-limit:] if after is None else events[:limit]
    events = [{k: _clip(v) for k, v in e.items()} for e in events]
    next_after = events[-1]["seq"] if events else (after or 0)
    job = _slim_job(meta)
    job["seq"] = meta.get("seq")
    return {"job": job, "events": events, "next_after": next_after}


@tool(
    "list_runs",
    "The lifetime run ledger: one row per command invocation (CLI, web and agent alike) "
    "with status, timings and the parsed result stats (counts of what the run did). "
    "Optionally filter by command name (sync, extract, cluster, recluster, apply, ...). "
    "Newest first. Scope: read.",
    properties={
        "command": {"type": "string"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
    },
)
def list_runs(ctx, args):
    command = arg_str(args, "command")
    limit = arg_int(args, "limit", 20, lo=1, hi=100)
    where, params = ("WHERE command = ?", [command]) if command else ("", [])
    c = ctx.conn()
    try:
        rows = c.execute(
            "SELECT id, command, origin, job_id, started_at, finished_at, status, stats_json "
            f"FROM command_runs {where} ORDER BY id DESC LIMIT ?",
            [*params, limit],
        ).fetchall()
    finally:
        c.close()
    runs = []
    for row in rows:
        try:
            stats = json.loads(row["stats_json"]) if row["stats_json"] else None
        except (ValueError, TypeError):
            stats = None
        runs.append(
            {
                **{k: row[k] for k in row.keys() if k != "stats_json"},
                "stats": stats,
            }
        )
    return {"runs": runs}


def _trim_payload(payload: dict) -> dict:
    slim = {k: v for k, v in payload.items() if k != "evidence"}
    evidence = payload.get("evidence")
    if isinstance(evidence, dict):
        slim["evidence"] = {k: v for k, v in evidence.items() if k != "exemplars"}
    return slim


@tool(
    "list_review_items",
    "List review-queue suggestions (assign, low_confidence, reassign, merge, merge_named, "
    "new_person, ...). Items about photos deleted on the NAS are left out. Each item has "
    "its payload (without bulky evidence exemplars) and the face_ids it speaks for; fetch "
    "pictures with get_face_crops. status defaults to 'pending'; use 'any' for all. "
    "Scope: read.",
    properties={
        "kind": {"type": "string"},
        "status": {
            "type": "string",
            "description": "pending, approved, rejected, hidden, applied, failed or any.",
            "default": "pending",
        },
        "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
        "offset": {"type": "integer", "minimum": 0, "default": 0},
    },
)
def list_review_items(ctx, args):
    kind = arg_str(args, "kind") or ""
    status = arg_str(args, "status", "pending")
    status = "" if status == "any" else status
    limit = arg_int(args, "limit", 50, lo=1, hi=200)
    offset = arg_int(args, "offset", 0, lo=0)
    c = ctx.conn()
    try:
        voided = ctx.voided.get(c)
        where, params = queries._where(kind, status, voided)
        rows = c.execute(
            "SELECT item_id, kind, payload_json, confidence, status, decided_at, decided_by "
            f"FROM review_queue {where} ORDER BY item_id LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        total = queries.count_review_items(c, kind=kind, status=status, voided=voided)
    finally:
        c.close()
    items = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except (ValueError, TypeError):
            payload = {}
        items.append(
            {
                "item_id": row["item_id"],
                "kind": row["kind"],
                "status": row["status"],
                "confidence": row["confidence"],
                "decided_at": row["decided_at"],
                "decided_by": row["decided_by"],
                "face_ids": sorted(queries.payload_face_ids(payload)),
                "payload": _trim_payload(payload),
            }
        )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@tool(
    "get_review_counts",
    "Review-queue counts as {status: {kind: count}} plus per-status totals, with items "
    "about deleted photos excluded. Scope: read.",
)
def get_review_counts(ctx, args):
    c = ctx.conn()
    try:
        counts = queries.queue_counts(c, voided=ctx.voided.get(c))
    finally:
        c.close()
    return {
        "counts": counts,
        "totals": {status: sum(kinds.values()) for status, kinds in counts.items()},
    }


def _run_cli(*cli_args: str) -> dict:
    env = {**os.environ, "SYNOPTICON_JOB_ORIGIN": "mcp"}
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "synopticon", *cli_args],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
            env=env,
        )
    except subprocess.TimeoutExpired:
        raise ToolError(f"'synopticon {' '.join(cli_args)}' did not finish within {_SUBPROCESS_TIMEOUT}s")
    text = proc.stdout + (("\n" + proc.stderr) if proc.stderr.strip() else "")
    return {"exit_code": proc.returncode, "output": _clip(text, _OUTPUT_CAP)}


@tool(
    "get_environment",
    "Hardware and runtime diagnostics for face detection and grouping (CPU/GPU, "
    "onnxruntime providers, library versions, thread counts), plus which model files "
    "are present. Runs the 'hwinfo' command in a subprocess because the web process "
    "never loads the inference stack. Scope: read.",
)
def get_environment(ctx, args):
    from ...pipeline.manifest import model_status

    result = _run_cli("hwinfo")
    try:
        result["models"] = model_status(ctx.settings.storage.models_dir)
    except Exception as exc:  # noqa: BLE001 - a broken manifest must not hide the hwinfo text
        result["models_error"] = f"{type(exc).__name__}: {exc}"
    return result


@tool(
    "check_nas",
    "Read-only probe of the NAS connection and credentials (runs 'synopticon check' in a "
    "subprocess). Returns its text output and exit code (0 means every step passed). "
    "Use it when Sync fails or nothing is syncing. Scope: read.",
)
def check_nas(ctx, args):
    return _run_cli("check")


def _crop_file(settings: Settings, stored: str | None) -> Path | None:
    if not stored:
        return None
    root = Path(settings.storage.crops_dir).resolve()
    path = Path(stored).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return None
    return path


@tool(
    "get_face_crops",
    "Look at faces: returns the aligned 112px face crop (or, with context=true, the wider "
    "~256px context crop) for up to 12 face ids, each captioned with the face id, "
    "detector score and quality. Get face ids from review items and the diagnostics "
    "tools. Scope: read.",
    properties={
        "face_ids": {
            "type": "array",
            "items": {"type": "integer"},
            "minItems": 1,
            "maxItems": 12,
        },
        "context": {"type": "boolean", "default": False},
    },
    required=["face_ids"],
)
def get_face_crops(ctx, args):
    ids = args.get("face_ids")
    if not isinstance(ids, list) or not 1 <= len(ids) <= 12:
        raise ToolError("'face_ids' must be a list of 1 to 12 integers")
    if any(isinstance(i, bool) or not isinstance(i, int) for i in ids):
        raise ToolError("'face_ids' must be integers")
    wide = arg_bool(args, "context", False)
    column = "ctx_crop_path" if wide else "crop_path"
    c = ctx.conn()
    try:
        rows = {
            int(r["face_id"]): r
            for r in c.execute(
                f"SELECT face_id, space, photo_id, det_score, quality, {column} AS path "
                f"FROM faces WHERE face_id IN ({','.join('?' * len(ids))})",
                ids,
            )
        }
    finally:
        c.close()
    images: list[ImageContent] = []
    missing: list[dict] = []
    for face_id in ids:
        row = rows.get(face_id)
        if row is None:
            missing.append({"face_id": face_id, "reason": "no such face"})
            continue
        path = _crop_file(ctx.settings, row["path"])
        if path is None:
            missing.append({"face_id": face_id, "reason": "crop image is missing (try job 'regen-crops')"})
            continue
        score = row["det_score"]
        quality = row["quality"]
        images.append(
            ImageContent(
                data=path.read_bytes(),
                mime_type=mimetypes.guess_type(path.name)[0] or "image/jpeg",
                caption=(
                    f"face {face_id} ({row['space']} photo {row['photo_id']}): "
                    f"det_score={'n/a' if score is None else round(score, 3)} "
                    f"quality={'n/a' if quality is None else round(quality, 2)}"
                ),
            )
        )
    return ToolOutput(structured={"returned": len(images), "missing": missing}, images=images)


# --------------------------------------------------------------------------- #
# Write tools                                                                   #
# --------------------------------------------------------------------------- #


def _before_after(before: Settings, after: Settings, changes: dict) -> dict:
    b, a = before.model_dump(mode="json"), after.model_dump(mode="json")
    return {
        f"{section}.{key}": {"before": b[section][key], "after": a[section][key]}
        for section, keys in changes.items()
        for key in keys
        if b[section][key] != a[section][key]
    }


def _pipeline_version(settings: Settings) -> str | None:
    from ...pipeline.manifest import load_manifest
    from ...pipeline.version import pipeline_version

    models_dir = settings.storage.models_dir
    if not load_manifest(models_dir):
        return None
    return pipeline_version(settings, models_dir)


@tool(
    "update_config",
    "Change settings in config.toml. 'changes' is {section: {key: value}} and only "
    "sections inference, detection, restoration, clustering and crossref are writable "
    "(never nas, storage, database, security or mcp). Refused while any job is queued or "
    "running. If the change alters the pipeline_version (any [detection] change), every "
    "photo would be re-detected on the next Detect faces run and review suggestions made "
    "before it would be orphaned, so it is refused unless confirm_redetect is true. Use "
    "get_config for keys, types and bounds. Grouping changes take effect with job "
    "'recluster'; detection changes with job 'extract'. Returns before/after per key. "
    "Scope: config.",
    properties={
        "changes": {
            "type": "object",
            "description": "{section: {key: value}}, e.g. {\"clustering\": {\"edge_threshold\": 0.55}}.",
            "additionalProperties": {"type": "object"},
        },
        "confirm_redetect": {"type": "boolean", "default": False},
    },
    required=["changes"],
    scopes=(CONFIG,),
    read_only=False,
)
def update_config(ctx, args):
    ctx.require(CONFIG)
    changes = args.get("changes")
    confirm_redetect = arg_bool(args, "confirm_redetect", False)
    if not isinstance(changes, dict) or not changes:
        raise ToolError("'changes' must be a non-empty object of {section: {key: value}}")
    for section, keys in changes.items():
        if section not in WRITABLE_SECTIONS:
            raise ToolError(
                f"section {section!r} is not writable through MCP; writable sections: "
                f"{', '.join(WRITABLE_SECTIONS)}. nas, storage, database, security and mcp "
                "are human-only."
            )
        if not isinstance(keys, dict) or not keys:
            raise ToolError(f"changes[{section!r}] must be a non-empty object of {{key: value}}")

    before = load_settings()
    for section, keys in changes.items():
        model = getattr(before, section)
        known = set(type(model).model_fields)
        for key in keys:
            if key not in known:
                raise ToolError(
                    f"unknown key {section}.{key}; keys: {', '.join(sorted(known))}"
                )
            if isinstance(getattr(model, key), (SecretStr, BaseModel)):
                raise ToolError(f"{section}.{key} cannot be changed through MCP")

    if _job_running(ctx.job_manager):
        raise ToolError("a job is queued or running; change settings once it finishes")

    if "detection" in changes:
        try:
            det = type(before.detection)(**{**before.detection.model_dump(), **changes["detection"]})
        except ValidationError as exc:
            raise ToolError(
                "invalid value: "
                + "; ".join(
                    f"detection.{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
                )
            )
        version_before = _pipeline_version(before)
        version_after = _pipeline_version(before.model_copy(update={"detection": det}))
        if version_before != version_after and not confirm_redetect:
            raise ToolError(
                "this changes the pipeline_version, so every photo would be re-detected on the "
                "next Detect faces run (job 'extract'), and review suggestions made before it "
                "would point at faces that no longer exist. If that is intended, call again "
                "with confirm_redetect=true."
            )

    errors = configio.write_config(ctx.settings, changes)
    if errors:
        raise ToolError(
            "invalid settings: " + "; ".join(f"{e['loc']}: {e['msg']}" for e in errors)
        )
    after = load_settings()
    log.info("config changed by %s: %s", ctx.actor, json.dumps(changes, default=str))

    changed = _before_after(before, after, changes)
    touched = {f"{section}.{key}" for section, keys in changes.items() for key in keys}
    shadowed = [k for k in configio._env_overrides(after) if k in touched]
    result: dict[str, Any] = {
        "changed": changed,
        "pipeline_version_before": _pipeline_version(before),
        "pipeline_version_after": _pipeline_version(after),
    }
    if shadowed:
        result["warnings"] = [
            f"{k} is set by a SYNOPTICON_* environment variable, which overrides config.toml; "
            "this edit has no effect until that variable is removed."
            for k in shadowed
        ]
    return result


_JOB_HELP = (
    "Start a pipeline step as a background job and get its id (poll get_job). Jobs run "
    "one at a time in a queue of at most 5. Jobs and params:\n"
    "- sync: space, hash(bool), skip_faces(bool), all_faces(bool), resume(bool). Pull the library from the NAS.\n"
    "- extract: limit(int), photo_id(int), space, retry_skipped(bool). Detect faces.\n"
    "- cluster: none. Group faces from scratch (re-runs crossref).\n"
    "- recluster: overrides({'clustering.edge_threshold': 0.55, 'crossref.majority': 0.6}); try "
    "grouping values without saving them to config.toml.\n"
    "- report: run_id(int). Build the HTML report.\n"
    "- regen-crops: space, only_missing(bool), limit(int). Rebuild missing face crops.\n"
    "- benchmark: limit, space, warmup. Time the detection models.\n"
    "- models-download: only(list of model keys), allow_record_hash(bool).\n"
    "- apply: dry_run(bool, DEFAULT true: a preview that writes nothing), kinds(list of assign, "
    "low_confidence, reassign, merge, new_person), person_id(int), space, plus apply_reassigns(bool) "
    "when kinds has reassign and apply_merges(bool) when kinds has merge. A real apply "
    "(dry_run=false) writes approved items to the NAS and needs the apply scope (apply:reassign, "
    "apply:merge for those kinds). merge_named is human-only.\n"
    "- dedupe: exact(bool), visual(bool), threshold(0-64), space. Dry-run only here; deleting "
    "duplicates is human-only.\n"
    "- clear-queue, clear-applies, delete-crops: none. prune-queue: include_approved(bool). "
    "reset: keep_crops(bool). These need the maintenance scope. reset with all=true is human-only.\n"
    "Everything before a real apply is read-only toward the NAS."
)


def _consent_message(exc: ConsentError) -> str:
    if exc.requirement == "phrase":
        return (
            "this needs a typed confirmation phrase, which only a human can give in the web "
            "GUI; it is not available through MCP"
        )
    if exc.requirement == "flag":
        return (
            f"also set params.{exc.field}=true to confirm you intend this "
            f"({exc.detail or exc.field}); it needs the matching apply scope"
        )
    return f"consent required ({exc.field})"


def _required_scopes(job: str, argv: list[str]) -> list[str]:
    if job in MAINTENANCE_JOBS:
        return [MAINTENANCE]
    if "--apply" in argv:
        needed = [APPLY]
        if "--apply-reassigns" in argv:
            needed.append(APPLY_REASSIGN)
        if "--apply-merges" in argv:
            needed.append(APPLY_MERGE)
        return needed
    return [JOBS]


@tool(
    "run_job",
    _JOB_HELP,
    properties={
        "job": {"type": "string", "enum": sorted(JOB_SPECS)},
        "params": {"type": "object", "default": {}},
    },
    required=["job"],
    scopes=(JOBS, MAINTENANCE, APPLY),
    read_only=False,
    destructive=True,
)
def run_job(ctx, args):
    job = arg_str(args, "job")
    params = args.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ToolError("'params' must be an object")
    try:
        argv = resolve_argv(job, params, confirm=True, confirm_phrase=None)
    except ConsentError as exc:
        raise ToolError(_consent_message(exc))
    except JobParamError as exc:
        raise ToolError(str(exc))
    for scope in _required_scopes(job, argv):
        ctx.require(scope, f"needed to run '{job}' with these parameters")
    try:
        job_id = ctx.job_manager.submit(
            job, params, confirm=True, confirm_phrase=None, origin="mcp"
        )
    except (JobParamError, QueueFullError) as exc:
        raise ToolError(str(exc))
    except ConsentError as exc:
        raise ToolError(_consent_message(exc))
    log.info("job %s (%s) started by %s", job_id, job, ctx.actor)
    return {"job_id": job_id, "job": job, "argv": argv}


@tool(
    "cancel_job",
    "Cancel a queued or running job. Scope: jobs.",
    properties={"job_id": {"type": "string"}},
    required=["job_id"],
    scopes=(JOBS,),
    read_only=False,
    destructive=True,
)
def cancel_job(ctx, args):
    ctx.require(JOBS)
    job_id = arg_str(args, "job_id")
    if not job_id or not ctx.job_manager.cancel(job_id):
        raise ToolError(f"job {job_id!r} is unknown or already finished")
    log.info("job %s cancelled by %s", job_id, ctx.actor)
    return {"job_id": job_id, "cancelled": True}


@tool(
    "decide_review_items",
    "Decide review-queue suggestions in bulk (up to 500 ids): approve, reject, hide, or "
    "'pending' to undo an earlier decision. Nothing reaches the NAS until a real Apply "
    "runs. Rejected suggestions are proposed again by the next Group faces run, hidden "
    "ones are not. Items already applied or failed, and items about photos deleted on "
    "the NAS, are skipped. Returns the outcome per item. Scope: review.",
    properties={
        "item_ids": {"type": "array", "items": {"type": "integer"}, "minItems": 1, "maxItems": 500},
        "decision": {"type": "string", "enum": list(_REVIEW_DECISIONS)},
    },
    required=["item_ids", "decision"],
    scopes=(REVIEW,),
    read_only=False,
)
def decide_review_items(ctx, args):
    ctx.require(REVIEW)
    ids = args.get("item_ids")
    decision = arg_str(args, "decision", choices=_REVIEW_DECISIONS)
    if not isinstance(ids, list) or not 1 <= len(ids) <= 500:
        raise ToolError("'item_ids' must be a list of 1 to 500 integers")
    if any(isinstance(i, bool) or not isinstance(i, int) for i in ids):
        raise ToolError("'item_ids' must be integers")
    ids = list(dict.fromkeys(ids))

    c = ctx.conn()
    outcomes: list[dict] = []
    try:
        voided = ctx.voided.get(c)
        statuses: dict[int, str] = {}
        for start in range(0, len(ids), 200):
            chunk = ids[start : start + 200]
            for row in c.execute(
                f"SELECT item_id, status FROM review_queue WHERE item_id IN ({','.join('?' * len(chunk))})",
                chunk,
            ):
                statuses[int(row["item_id"])] = row["status"]
        for item_id in ids:
            status = statuses.get(item_id)
            if status is None:
                outcomes.append({"item_id": item_id, "outcome": "not_found"})
            elif item_id in voided:
                outcomes.append({"item_id": item_id, "outcome": "skipped_deleted_photo", "status": status})
            elif status not in _DECIDABLE:
                outcomes.append({"item_id": item_id, "outcome": "skipped_final", "status": status})
            else:
                try:
                    if decision == "pending":
                        new = queries.undo_decision(c, item_id)
                    else:
                        new = queries.decide_item(c, item_id, decision, decided_by=ctx.actor)
                except db_errors.DatabaseError:
                    c.rollback()
                    outcomes.append({"item_id": item_id, "outcome": "error", "status": status})
                    continue
                if new is None:
                    outcomes.append({"item_id": item_id, "outcome": "not_undoable", "status": status})
                else:
                    outcomes.append({"item_id": item_id, "outcome": "ok", "status": new})
    finally:
        c.close()
    tally: dict[str, int] = {}
    for o in outcomes:
        tally[o["outcome"]] = tally.get(o["outcome"], 0) + 1
    log.info("review decision %s on %d items by %s: %s", decision, len(ids), ctx.actor, tally)
    return {"decision": decision, "tally": tally, "items": outcomes}


TOOLS = [
    get_overview,
    get_config,
    list_jobs,
    get_job,
    list_runs,
    list_review_items,
    get_review_counts,
    get_environment,
    check_nas,
    get_face_crops,
    update_config,
    run_job,
    cancel_job,
    decide_review_items,
]
