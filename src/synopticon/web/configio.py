"""Config editing for the web GUI: TOML round-trip + validation + secret masking.

`config.toml` is edited through :mod:`tomlkit` so comments and key/section order
survive a save (a plain ``dump(load())`` would flatten the file). tomlkit is
lazy-imported behind :func:`_require_tomlkit` so the package still imports without
the ``[review]`` extra.

Design (see the web-GUI plan §4):

* :func:`config_target` resolves the file we read/write: ``$SYNOPTICON_CONFIG`` →
  the path :func:`synopticon.config._config_file` discovers → the default
  ``<data_dir>/config.toml`` for a fresh install.
* :func:`read_config` (``GET /api/config``) returns the effective values with
  every :class:`~pydantic.SecretStr` masked to ``{"secret": True, "set": bool}``
  — the password plaintext is *never* serialized out — plus the JSON schema and
  the list of keys currently shadowed by ``SYNOPTICON_*`` env / ``.env`` vars.
* :func:`write_config` (``PUT /api/config``) merges only the changed keys into
  the existing tomlkit document (dropping absent/``"__unchanged__"`` secrets),
  validates the merged result via :class:`~synopticon.config.Settings`, and on
  success writes atomically (temp + ``os.replace``), tightening the file to
  ``0600`` whenever a password is present.
"""

import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, SecretStr, ValidationError

from ..config import Settings, _config_file

#: Sentinel a client sends for a secret field it does not want to change.
_UNCHANGED = "__unchanged__"


def _require_tomlkit():
    try:
        import tomlkit  # noqa: F401
    except ImportError as exc:  # pragma: no cover - trivial guard
        raise ImportError(
            "Config editing needs the [review] extra: pip install 'synopticon[review]'"
        ) from exc
    return tomlkit


# --------------------------------------------------------------------------- #
# Path resolution                                                              #
# --------------------------------------------------------------------------- #
def config_target(settings: Settings) -> Path:
    """The config file we read from / write to.

    ``$SYNOPTICON_CONFIG`` and the discovered ``_config_file()`` both win when
    present; otherwise a fresh install writes to ``<data_dir>/config.toml`` (a
    location in the search path for both bare-metal and Docker layouts).
    """
    discovered = _config_file()
    if discovered is not None:
        return discovered
    return Path(settings.storage.data_dir) / "config.toml"


# --------------------------------------------------------------------------- #
# GET: read + mask                                                             #
# --------------------------------------------------------------------------- #
def _mask_secrets(model: BaseModel, dumped: dict) -> None:
    """Replace every SecretStr in ``dumped`` with a set/unset marker in place.

    Walks the pydantic model (not the dumped dict) so masking is keyed on the
    real field types, and recurses through nested models (nas/storage/...).
    """
    for name in type(model).model_fields:
        value = getattr(model, name)
        if isinstance(value, SecretStr):
            dumped[name] = {"secret": True, "set": bool(value.get_secret_value())}
        elif isinstance(value, BaseModel) and isinstance(dumped.get(name), dict):
            _mask_secrets(value, dumped[name])


def _dump_masked(settings: Settings) -> dict:
    data = settings.model_dump(mode="json")
    _mask_secrets(settings, data)
    return data


def _read_dotenv(path: Path) -> dict[str, str]:
    """Best-effort parse of a ``.env`` file (KEY=VALUE lines) → dict."""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, _val = line.partition("=")
        key = key.strip()
        if key.lower().startswith("export "):
            key = key[len("export ") :].strip()
        if key:
            out[key] = _val
    return out


def _env_overrides(settings: Settings) -> list[str]:
    """Dotted ``section.key`` names shadowed by a ``SYNOPTICON_*`` env / .env var.

    A TOML edit to a shadowed key has no effect while the env var is set; the UI
    surfaces this so the user is not surprised.
    """
    prefix = Settings.model_config.get("env_prefix", "SYNOPTICON_")
    delim = Settings.model_config.get("env_nested_delimiter", "__")
    env_file = Settings.model_config.get("env_file", ".env")

    present = {k.upper() for k in os.environ}
    if env_file:
        present |= {k.upper() for k in _read_dotenv(Path(env_file))}

    out: list[str] = []
    for section_name in type(settings).model_fields:
        section = getattr(settings, section_name)
        if not isinstance(section, BaseModel):
            continue
        for key in type(section).model_fields:
            var = f"{prefix}{section_name}{delim}{key}".upper()
            if var in present:
                out.append(f"{section_name}.{key}")
    return out


def read_config(settings: Settings) -> dict:
    """The payload for ``GET /api/config`` — values (masked), schema, overrides."""
    target = config_target(settings)
    return {
        "path": str(target),
        "exists": target.is_file(),
        "values": _dump_masked(settings),
        "schema": Settings.model_json_schema(),
        "env_overrides": _env_overrides(settings),
    }


# --------------------------------------------------------------------------- #
# PUT: clean + merge + validate + write                                        #
# --------------------------------------------------------------------------- #
def _is_masked_secret(value: Any) -> bool:
    """True for the echoed ``{"secret": True, "set": ...}`` GET marker."""
    return (
        isinstance(value, dict)
        and value.get("secret") is True
        and set(value.keys()) <= {"secret", "set"}
    )


def _clean(partial: dict) -> dict:
    """Drop ``"__unchanged__"`` / masked-secret placeholders, recursively.

    An unchanged secret is simply absent from the merge, so the value already on
    disk is preserved verbatim.
    """
    out: dict[str, Any] = {}
    for key, value in partial.items():
        if value == _UNCHANGED or _is_masked_secret(value):
            continue
        if isinstance(value, dict):
            sub = _clean(value)
            if sub:
                out[key] = sub
        else:
            out[key] = value
    return out


def _deep_merge(container, partial: dict, tomlkit) -> None:
    """Merge ``partial`` into a tomlkit container, creating tables as needed.

    Existing tables are reused (never replaced) so their comments and key order
    are preserved; only touched leaves are rewritten.
    """
    for key, value in partial.items():
        if isinstance(value, dict):
            existing = container.get(key)
            if not isinstance(existing, dict):
                existing = tomlkit.table()
                container[key] = existing
            _deep_merge(existing, value, tomlkit)
        else:
            container[key] = value


def _has_password(doc) -> bool:
    nas = doc.get("nas")
    return isinstance(nas, dict) and bool(nas.get("password"))


def write_config(settings: Settings, partial: dict) -> list[dict] | None:
    """Apply a partial nested config edit. Returns ``None`` on success.

    On validation failure returns a list of ``{"loc": "section.key", "msg": ...}``
    (the caller maps this to HTTP 422). Comments/order are preserved via tomlkit;
    the write is atomic (temp + ``os.replace``) and tightened to ``0600`` when a
    password is present in the resulting file.
    """
    tomlkit = _require_tomlkit()
    cleaned = _clean(partial or {})
    target = config_target(settings)

    if target.is_file():
        doc = tomlkit.parse(target.read_text(encoding="utf-8"))
    else:
        doc = tomlkit.document()

    _deep_merge(doc, cleaned, tomlkit)

    # Validate the merged file content. Init kwargs win over env/.env/defaults,
    # so we are validating exactly what will be written.
    merged_plain = doc.unwrap()
    try:
        Settings(**merged_plain)
    except ValidationError as exc:
        return [
            {
                "loc": ".".join(str(p) for p in err.get("loc", ())),
                "msg": err.get("msg", "invalid value"),
            }
            for err in exc.errors()
        ]

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(tomlkit.dumps(doc), encoding="utf-8")
    if _has_password(doc):
        os.chmod(tmp, 0o600)
    os.replace(tmp, target)
    return None


# --------------------------------------------------------------------------- #
# Routes                                                                       #
# --------------------------------------------------------------------------- #
def register_config_routes(app, settings: Settings, conn, job_manager) -> None:
    """Wire the config + access (auth) API onto an existing FastAPI ``app``.

    ``conn`` is the per-request ``sqlite3.Connection`` factory and ``job_manager``
    the running :class:`~synopticon.web.jobs.JobManager` (a running job blocks a
    config write). Called once from ``create_app`` right before it returns.
    """
    from fastapi import Request
    from fastapi.responses import JSONResponse
    from starlette.concurrency import run_in_threadpool

    from . import auth

    def _job_running() -> bool:
        return any(
            j.get("state") in ("queued", "running") for j in job_manager.list_jobs()
        )

    def _drop_cached_auth() -> None:
        """Make a credential change take effect on the very next request.

        ``create_app``'s auth middleware caches validated credentials briefly;
        without this, a revoked key or a changed password would keep working
        until the entry expired.
        """
        invalidate = getattr(app.state, "invalidate_auth_cache", None)
        if invalidate is not None:
            invalidate()

    # -- config ------------------------------------------------------------- #
    @app.get("/api/config")
    def api_get_config():
        return read_config(settings)

    @app.put("/api/config")
    async def api_put_config(request: Request):
        if _job_running():
            return JSONResponse(
                {"error": "a job is running; save config once it finishes"},
                status_code=409,
            )
        body = await request.json()
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": "body must be a config object"}, status_code=422
            )
        # Rewrites config.toml (tomlkit round-trip + file write) — off the loop.
        errors = await run_in_threadpool(write_config, settings, body)
        if errors:
            return JSONResponse({"errors": errors}, status_code=422)
        return {"ok": True}

    # -- access: password + API keys ---------------------------------------- #
    @app.post("/api/auth/change-password")
    async def api_change_password(request: Request):
        ident = getattr(request.state, "ident", None)
        if not ident or ident[0] != "user":
            return JSONResponse(
                {"error": "must be signed in to change the password"}, status_code=403
            )
        uid = ident[1]
        body = await request.json()
        current = body.get("current_password") or ""
        new = body.get("new_password") or ""
        if not new:
            return JSONResponse(
                {"error": "new_password is required"}, status_code=422
            )

        def work() -> str:
            c = conn()
            try:
                row = c.execute(
                    "SELECT username FROM web_users WHERE id = ?", (uid,)
                ).fetchone()
                if row is None:
                    return "unknown"
                if auth.verify_password(c, row["username"], current) is None:
                    return "wrong"
                auth.change_password(c, uid, new)
                return "ok"
            finally:
                c.close()

        # Two scrypt derivations (verify + rehash) — ~200 ms of CPU that must
        # not run on the event loop.
        outcome = await run_in_threadpool(work)
        if outcome == "unknown":
            return JSONResponse({"error": "unknown user"}, status_code=404)
        if outcome == "wrong":
            return JSONResponse(
                {"error": "current password is incorrect"}, status_code=403
            )
        _drop_cached_auth()
        return {"ok": True}

    @app.get("/api/auth/keys")
    def api_list_keys():
        c = conn()
        try:
            return {"keys": auth.list_api_keys(c)}
        finally:
            c.close()

    @app.post("/api/auth/keys")
    async def api_create_key(request: Request):
        body = await request.json()
        name = (body.get("name") or "").strip()
        if not name:
            return JSONResponse({"error": "name is required"}, status_code=422)

        def work():
            c = conn()
            try:
                return auth.create_api_key(c, name)
            finally:
                c.close()

        key = await run_in_threadpool(work)
        # The plaintext key is shown exactly once — only its hash is stored.
        return JSONResponse({"key": key, "name": name}, status_code=201)

    @app.post("/api/auth/keys/{key_id}/revoke")
    def api_revoke_key(key_id: int):
        c = conn()
        try:
            auth.revoke_api_key(c, key_id)
        finally:
            c.close()
        _drop_cached_auth()
        return {"ok": True}
