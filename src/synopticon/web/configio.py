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
* :func:`export_config` (``GET /api/backup/config``, in ``backup_routes``) hands
  the file back as a download. It is the one path that can serialize a plaintext
  secret, and only when the caller opts in — the default blanks them.
"""

import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, SecretStr, ValidationError

from ..config import Settings, _config_file
from . import clientip

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
# Backup: the file itself, credentials optional                                #
# --------------------------------------------------------------------------- #
def secret_paths(settings: Settings) -> list[tuple[str, str]]:
    """``(section, key)`` for every :class:`~pydantic.SecretStr` field."""
    out: list[tuple[str, str]] = []
    for section_name in type(settings).model_fields:
        section = getattr(settings, section_name)
        if not isinstance(section, BaseModel):
            continue
        for key in type(section).model_fields:
            if isinstance(getattr(section, key), SecretStr):
                out.append((section_name, key))
    return out


def _drop_none(data: dict) -> dict:
    """Recursively strip ``None`` values — TOML has no null to write them as."""
    out: dict[str, Any] = {}
    for key, value in data.items():
        if value is None:
            continue
        out[key] = _drop_none(value) if isinstance(value, dict) else value
    return out


def _plain_values(settings: Settings, include_secrets: bool) -> dict:
    """``model_dump`` with the secrets substituted back in (or blanked).

    ``model_dump(mode="json")`` renders a ``SecretStr`` as ``'**********'``,
    which would be written to the backup as if it were the password.
    """
    data = _drop_none(settings.model_dump(mode="json"))
    for section, key in secret_paths(settings):
        value: SecretStr = getattr(getattr(settings, section), key)
        data.setdefault(section, {})[key] = (
            value.get_secret_value() if include_secrets else ""
        )
    return data


def export_config(settings: Settings, *, include_secrets: bool = False) -> str:
    """The TOML text of a settings backup.

    Normally a verbatim copy of ``config.toml`` — comments, order and all — so
    restoring is a file copy. With ``include_secrets`` false (the default) the
    secret fields are blanked in place, which keeps the shape of the file while
    honouring the rule that plaintext credentials are not serialized out of the
    process unless the operator asked for them explicitly.

    An install configured purely through environment variables has no file to
    copy; it gets the effective settings rendered as TOML instead, so the backup
    is still a usable starting point.
    """
    tomlkit = _require_tomlkit()
    target = config_target(settings)

    if target.is_file():
        doc = tomlkit.parse(target.read_text(encoding="utf-8"))
        if not include_secrets:
            for section, key in secret_paths(settings):
                table = doc.get(section)
                if isinstance(table, dict) and key in table:
                    table[key] = ""
                    table[key].comment("redacted by settings backup")
    else:
        doc = tomlkit.document()
        _deep_merge(doc, _plain_values(settings, include_secrets), tomlkit)

    return tomlkit.dumps(doc)


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


def _atomic_write(doc, target: Path, tomlkit) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(tomlkit.dumps(doc), encoding="utf-8")
    if _has_password(doc):
        os.chmod(tmp, 0o600)
    os.replace(tmp, target)


def _env_var_name(dotted: str) -> str:
    """``"security.allow_from"`` -> ``"SYNOPTICON_SECURITY__ALLOW_FROM"``."""
    section, _, key = dotted.partition(".")
    prefix = Settings.model_config.get("env_prefix", "SYNOPTICON_")
    delim = Settings.model_config.get("env_nested_delimiter", "__")
    return f"{prefix}{section}{delim}{key}".upper()


def guarded_write(
    settings: Settings,
    partial: dict,
    *,
    peer: str,
    forwarded_for: str,
    bind_host: str | None,
    allow_lockout: bool,
) -> tuple[list[dict] | None, dict | None]:
    """One worker-thread pass: read, merge, validate, guard, write.

    Returns ``(errors, conflict)``; at most one is non-``None``. ``errors`` ->
    the caller answers 422 with ``{"errors": errors}``; ``conflict`` -> 409 with
    that dict as the body; neither -> written.

    Judges the *proposed* trust list against the socket ``peer`` (not the
    address the currently-running ``ProxyHeaders`` middleware would resolve),
    because the realistic first-time save adds a proxy to ``trusted_proxies``
    and an allowlist entry in the same PUT -- judging against the old list
    would refuse exactly the save that fixes the configuration.
    """
    tomlkit = _require_tomlkit()
    target = config_target(settings)

    if target.is_file():
        doc = tomlkit.parse(target.read_text(encoding="utf-8"))
    else:
        doc = tomlkit.document()

    cleaned = _clean(partial or {})
    _deep_merge(doc, cleaned, tomlkit)

    merged_plain = doc.unwrap()
    try:
        merged = Settings(**merged_plain)
    except ValidationError as exc:
        return [
            {
                "loc": ".".join(str(p) for p in err.get("loc", ())),
                "msg": err.get("msg", "invalid value"),
            }
            for err in exc.errors()
        ], None

    sec = merged.security

    # Guard 0 -- environment shadow (F7). Not overridable by allow_lockout: it
    # means "I accept the lockout risk", not "write a file nobody reads".
    if "security" in (partial or {}):
        shadowed_security = [
            k for k in _env_overrides(settings) if k.startswith("security.")
        ]
        if shadowed_security:
            clauses = "; ".join(
                f"{key} is set by the environment variable {_env_var_name(key)}, "
                "which overrides config.toml"
                for key in shadowed_security
            )
            return None, {
                "error": (
                    f"{clauses} — saving here would change the file and nothing "
                    "else. Unset it (docker compose: remove the line from "
                    "`environment:`) and restart Synopticon."
                ),
                "env_shadowed": shadowed_security,
                "shadowed": True,
            }

    if allow_lockout:
        _atomic_write(doc, target, tomlkit)
        return None, None

    trusted = clientip.parse_networks(sec.trusted_proxies)
    resolved = clientip.resolve_client(peer, forwarded_for, trusted)

    # Guard 1 -- unenforceable: untrusted proxy. R8: no loopback-peer carve-out
    # here -- the documented topology (proxy on the same host) IS loopback, so
    # that clause used to disable the one check built to catch exactly this.
    if sec.allow_from and not sec.trusted_proxies and forwarded_for:
        return None, {
            "error": (
                "This request reached Synopticon through a proxy that is not in "
                "trusted_proxies, so every visitor looks like the same address "
                "and this list would not restrict anyone. Set trusted_proxies "
                "first."
            ),
            "client_ip": resolved.ip,
            "forwarded_for_present": True,
            "unenforceable": True,
        }

    # Guard 2 -- unenforceable: same-host proxy that forwards nothing. The
    # backstop covers a loopback-bound instance behind an unlisted same-host
    # proxy that sends no X-Forwarded-For at all.
    same_host_no_forward = (
        bool(sec.allow_from)
        and resolved.peer_trusted
        and resolved.source == "socket_peer"
        and clientip.is_loopback(resolved.ip)
    )
    backstop = (
        bool(sec.allow_from)
        and not sec.trusted_proxies
        and not forwarded_for
        and clientip.is_loopback(peer)
        and bind_host is not None
        and clientip.is_loopback(bind_host)
    )
    if same_host_no_forward or backstop:
        return None, {
            "error": (
                "Your reverse proxy runs on this machine and is not sending "
                "X-Forwarded-For, so every visitor arrives as 127.0.0.1 and this "
                "list cannot tell them apart. Configure the proxy to OVERWRITE "
                "that header (nginx: proxy_set_header X-Forwarded-For "
                "$remote_addr;) — passing the visitor's own header through "
                "instead would let anyone claim any address — and try again."
            ),
            "client_ip": resolved.ip,
            "unenforceable": True,
        }

    # Guard 3 -- self-lockout.
    allowlist = clientip.IPAllowlist(sec.allow_from, sec.allow_private_networks)
    if not allowlist.allows(resolved.ip):
        return None, {
            "error": "That allowlist would lock this browser out.",
            "client_ip": resolved.ip,
            "lockout": True,
        }

    _atomic_write(doc, target, tomlkit)
    return None, None


# --------------------------------------------------------------------------- #
# Routes                                                                       #
# --------------------------------------------------------------------------- #
def register_config_routes(app, settings: Settings, conn, job_manager) -> None:
    """Wire the config + access (auth) API onto an existing FastAPI ``app``.

    ``conn`` is the per-request ``Connection`` factory and ``job_manager``
    the running :class:`~synopticon.web.jobs.JobManager` (a running job blocks a
    config write). Called once from ``create_app`` right before it returns.
    """
    from fastapi import Request
    from fastapi.responses import JSONResponse
    from starlette.concurrency import run_in_threadpool

    from . import auth
    from .auth import SESSION_COOKIE

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

    def _human_only(request: Request) -> JSONResponse | None:
        """``None`` for a signed-in user, else a 403 for an API key.

        An API key is a machine credential and must never be able to widen its
        own reach: `[security]` owns the allowlist, the proxy trust list and
        both throttle tiers, and the key routes below mint and revoke keys. A
        key that could write either would be a stolen key that grants itself
        everything and outlives its own revocation. Mirrors
        ``security_routes._require_user``.
        """
        ident = getattr(request.state, "ident", None)
        if not ident or ident[0] != "user":
            return JSONResponse({"error": "must be signed in"}, status_code=403)
        return None

    # -- config ------------------------------------------------------------- #
    @app.get("/api/config")
    def api_get_config():
        return read_config(settings)

    @app.put("/api/config")
    async def api_put_config(request: Request):
        denied = _human_only(request)
        if denied is not None:
            return denied
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
        # `guarded_write` judges the proposed security section against the
        # SOCKET PEER: the running ProxyHeaders middleware was built from the
        # OLD trusted_proxies, so client_ip() would answer with the proxy's
        # address even when this very save is what adds that proxy to the
        # trust list.
        errors, conflict = await run_in_threadpool(
            guarded_write,
            settings,
            body,
            peer=clientip.client_peer(request),
            forwarded_for=clientip.forwarded_header(request),
            bind_host=getattr(request.app.state, "bind_host", None),
            allow_lockout=request.query_params.get("allow_lockout") == "1",
        )
        if conflict:
            return JSONResponse(conflict, status_code=409)
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

        # Computed on the loop (pure/in-memory); passed into the hop so the
        # limiter call and the log row use exactly what the middleware
        # resolved for this request (clientip.resolved(request), never a
        # second, independent re-resolution).
        resolved = clientip.resolved(request)
        ua = clientip.user_agent(request)
        limiter = request.app.state.login_limiter
        session_cookie = request.cookies.get(SESSION_COOKIE)

        def work() -> tuple[str, int | None]:
            c = conn()
            try:
                row = c.execute(
                    "SELECT username FROM web_users WHERE id = ?", (uid,)
                ).fetchone()
                if row is None:
                    return "unknown", None
                username = row["username"]

                # Throttle verdict computed inside the hop, after the username
                # lookup and before verify_password -- this is an authenticated
                # route, not an anonymous flood surface, so the one connection
                # a blocked request costs is acceptable and buys the correct
                # key (scope="change_password").
                throttle = limiter.verdict(resolved, username, scope="change_password")
                if not throttle.allowed:
                    return "blocked", throttle.retry_after

                if auth.verify_password(c, username, current) is None:
                    limiter.record_failure(resolved, username, scope="change_password")
                    auth.record_attempt(
                        c,
                        event="password_change",
                        outcome="failure",
                        reason="wrong_password",
                        username=username,
                        user_id=uid,
                        ip=resolved.ip,
                        user_agent=ua,
                    )
                    return "wrong", None

                auth.change_password(c, uid, new)
                limiter.record_success(resolved, username, scope="change_password")
                # Changing a password because a device was lost is the single
                # most likely reason anyone uses this route -- revoke every
                # other session so the thief's 30-day cookie does not outlive
                # the credential it was issued against.
                revoked = auth.delete_user_sessions(c, uid, except_token=session_cookie)
                auth.record_attempt(
                    c,
                    event="password_change",
                    outcome="success",
                    username=username,
                    user_id=uid,
                    ip=resolved.ip,
                    user_agent=ua,
                )
                return "ok", revoked
            finally:
                c.close()

        # Two scrypt derivations (verify + rehash) — ~200 ms of CPU that must
        # not run on the event loop.
        outcome, extra = await run_in_threadpool(work)
        if outcome == "blocked":
            return JSONResponse(
                {
                    "error": f"Too many attempts — try again in {extra} seconds.",
                    "retry_after": extra,
                    "recovery": (
                        "If this is your own address and you cannot get back in, "
                        "set [security] max_failures_per_address = 0 in "
                        "config.toml and restart Synopticon."
                    ),
                },
                status_code=429,
            )
        if outcome == "unknown":
            return JSONResponse({"error": "unknown user"}, status_code=404)
        if outcome == "wrong":
            return JSONResponse(
                {"error": "current password is incorrect"}, status_code=403
            )
        _drop_cached_auth()
        return {"ok": True, "signed_out_others": extra}

    @app.get("/api/auth/keys")
    def api_list_keys(request: Request):
        denied = _human_only(request)
        if denied is not None:
            return denied
        c = conn()
        try:
            return {"keys": auth.list_api_keys(c)}
        finally:
            c.close()

    @app.post("/api/auth/keys")
    async def api_create_key(request: Request):
        denied = _human_only(request)
        if denied is not None:
            return denied
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
    def api_revoke_key(key_id: int, request: Request):
        # A key may burn itself -- that is strictly de-escalating, and a CI job
        # retiring its own credential on teardown is a real workflow. It may
        # not revoke any OTHER key: that is a stolen key locking the owner out
        # of their own automation.
        ident = getattr(request.state, "ident", None)
        self_revoke = bool(ident) and ident[0] == "apikey" and ident[1] == key_id
        if not self_revoke:
            denied = _human_only(request)
            if denied is not None:
                return denied
        c = conn()
        try:
            auth.revoke_api_key(c, key_id)
        finally:
            c.close()
        _drop_cached_auth()
        return {"ok": True}
