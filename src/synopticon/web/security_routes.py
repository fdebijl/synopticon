"""``/api/security/*``: the instance's protection state (SEC2/SEC3/SEC5) plus
the re-authenticated ``/api/auth/totp/*`` and ``/api/auth/session-pinning``
routes (SEC1/SEC4).

Split out from ``app.py``/``configio.py`` because six new route surfaces would
otherwise collide across four owners; this file has a single owner instead. W0
created it with ``register_security_routes(...)``, the ``_drop_cached_auth``
helper and ``501`` stubs; WA replaces the stub bodies with the real
implementation from section 5 of the security contract.

Must NOT carry ``from __future__ import annotations`` -- FastAPI would degrade
the ``Request`` parameters below to required query fields and 422 every call,
exactly as it would in ``quickmerger.py``, ``schedule_routes.py``,
``backup_routes.py`` and ``inspect_routes.py``.
"""

from typing import Any


def _drop_cached_auth(request) -> None:
    """Make a credential change take effect on the very next request.

    Resolved through ``request.app.state`` AT CALL TIME, which is what makes
    the registration order irrelevant: ``register_security_routes`` is called
    from ``create_app`` before ``app.state.invalidate_auth_cache`` is
    assigned -- so capturing it at registration would bind ``None``. This is
    the same shape, and the same reason, as ``configio``'s own
    ``_drop_cached_auth``, which is a private closure of that module's own
    registrar and cannot be reached from here.
    """
    invalidate = getattr(request.app.state, "invalidate_auth_cache", None)
    if invalidate is not None:
        invalidate()


def register_security_routes(
    app: Any,
    settings: Any,
    conn: Any,
    limiter: Any,
    allowlist: Any,
    trusted: Any,
) -> None:
    """Wire ``/api/security/*`` and the re-auth ``/api/auth/*`` routes onto an
    existing FastAPI ``app``.

    ``conn`` is the per-request ``Connection`` factory; ``limiter`` and
    ``allowlist`` are the objects ``create_app`` publishes at
    ``app.state.login_limiter`` / ``app.state.ip_allowlist``; ``trusted`` is the
    parsed ``trusted_proxies`` network list. Called once from ``create_app``,
    right after ``register_config_routes``.
    """
    from fastapi import Request
    from fastapi.responses import JSONResponse
    from starlette.concurrency import run_in_threadpool

    from . import auth, clientip, totp

    def _require_user(request: Request) -> tuple[int | None, JSONResponse | None]:
        """``(uid, None)`` for a signed-in user, else ``(None, <403>)``.

        An API key is a machine credential: none of routes 10-20 are reachable
        with one (R12, C4) -- managing a factor, a pin or the instance's
        protection state needs a human who just re-typed a password.
        """
        ident = getattr(request.state, "ident", None)
        if not ident or ident[0] != "user":
            return None, JSONResponse({"error": "must be signed in"}, status_code=403)
        return ident[1], None

    def _throttled(t) -> JSONResponse:
        """The one 429 body used everywhere in this contract (section 5.4).

        The tier that fired is never named -- that would be an oracle -- and
        the recovery line always names ``max_failures_per_address``, the one
        config key that can turn off the address tier, even when a pair-tier
        block is what actually fired (a pair block clears on its own in
        ``pair_block_seconds`` and needs no escape hatch).
        """
        return JSONResponse(
            {
                "error": f"Too many attempts — try again in {t.retry_after} seconds.",
                "retry_after": t.retry_after,
                "recovery": (
                    "If this is your own address and you cannot get back in, set "
                    "[security] max_failures_per_address = 0 in config.toml and "
                    "restart Synopticon."
                ),
            },
            status_code=429,
            headers={"Retry-After": str(t.retry_after)},
        )

    # -- /api/auth/totp/* (routes 10-14) ------------------------------------ #

    @app.get("/api/auth/totp")
    def api_totp_status(request: Request):
        uid, err = _require_user(request)
        if err:
            return err
        c = conn()
        try:
            return auth.totp_status(c, uid)
        finally:
            c.close()

    @app.post("/api/auth/totp/start")
    async def api_totp_start(request: Request):
        uid, err = _require_user(request)
        if err:
            return err
        body = await request.json()
        password = body.get("password") or ""
        fresh = request.query_params.get("fresh") == "1"
        if not password:
            return JSONResponse({"error": "password is required"}, status_code=422)

        def work():
            c = conn()
            try:
                resolved = clientip.resolved(request)
                username = auth.username_for(c, uid)
                t = limiter.verdict(resolved, username, scope="reauth")
                if not t.allowed:
                    return ("blocked", t)
                if auth.verify_password(c, username, password) is None:
                    limiter.record_failure(resolved, username, scope="reauth")
                    auth.record_attempt(
                        c,
                        event="security_change",
                        outcome="failure",
                        reason="reauth_failed",
                        username=username,
                        user_id=uid,
                        ip=resolved.ip,
                        user_agent=clientip.user_agent(request),
                    )
                    return ("bad_password",)
                limiter.record_success(resolved, username, scope="reauth")
                try:
                    pending = auth.start_totp_enrolment(c, uid, fresh=fresh)
                except auth.TotpAlreadyEnrolledError:
                    return ("already_enrolled",)
                otpauth_uri = totp.provisioning_uri(
                    pending.secret, account=username, issuer=settings.security.totp_issuer
                )
                secret_groups = " ".join(
                    pending.secret[i : i + 4] for i in range(0, len(pending.secret), 4)
                )
                return ("ok", pending, otpauth_uri, secret_groups, username)
            finally:
                c.close()

        result = await run_in_threadpool(work)
        kind = result[0]
        if kind == "blocked":
            return _throttled(result[1])
        if kind == "bad_password":
            return JSONResponse({"error": "That password was not accepted."}, status_code=403)
        if kind == "already_enrolled":
            return JSONResponse(
                {"error": "two-step sign-in is already enrolled"}, status_code=409
            )
        _, pending, otpauth_uri, secret_groups, username = result
        return {
            "secret": pending.secret,
            "secret_groups": secret_groups,
            "otpauth_uri": otpauth_uri,
            "pending_expires_in": pending.pending_expires_in,
            "manual": {
                "issuer": settings.security.totp_issuer,
                "account": username,
                "algorithm": "SHA1",
                "digits": 6,
                "period": 30,
            },
        }

    @app.post("/api/auth/totp/confirm")
    async def api_totp_confirm(request: Request):
        uid, err = _require_user(request)
        if err:
            return err
        body = await request.json()
        code = (body.get("code") or "").strip()
        token = request.cookies.get(auth.SESSION_COOKIE)

        def work():
            c = conn()
            try:
                resolved = clientip.resolved(request)
                username = auth.username_for(c, uid)
                t = limiter.verdict(resolved, username, scope="reauth")
                if not t.allowed:
                    return ("blocked", t)
                try:
                    codes = auth.confirm_totp_enrolment(
                        c,
                        uid,
                        code,
                        skew=settings.security.totp_skew_steps,
                        recovery_count=settings.security.recovery_code_count,
                    )
                except auth.NoPendingEnrolmentError:
                    return ("expired",)
                if codes is None:
                    limiter.record_failure(resolved, username, scope="reauth")
                    auth.record_attempt(
                        c,
                        event="security_change",
                        outcome="failure",
                        reason="reauth_failed",
                        username=username,
                        user_id=uid,
                        ip=resolved.ip,
                        user_agent=clientip.user_agent(request),
                    )
                    return ("bad_code",)
                limiter.record_success(resolved, username, scope="reauth")
                # A newly-confirmed factor must not leave a device signed in
                # that predates it -- every other session is revoked, and the
                # caller's own cookie is dropped from the auth cache below.
                auth.delete_user_sessions(c, uid, except_token=token)
                auth.record_attempt(
                    c,
                    event="security_change",
                    outcome="success",
                    reason="totp_enrolled",
                    username=username,
                    user_id=uid,
                    ip=resolved.ip,
                    user_agent=clientip.user_agent(request),
                )
                return ("ok", codes)
            finally:
                c.close()

        result = await run_in_threadpool(work)
        kind = result[0]
        if kind == "blocked":
            return _throttled(result[1])
        if kind == "expired":
            return JSONResponse(
                {
                    "error": "Two-step sign-in setup has expired or was never "
                    "started — start again."
                },
                status_code=409,
            )
        if kind == "bad_code":
            return JSONResponse(
                {
                    "error": "That code was not accepted — check the clock on "
                    "your phone and on this server."
                },
                status_code=401,
            )
        _drop_cached_auth(request)
        _, codes = result
        return {"ok": True, "recovery_codes": codes}

    @app.post("/api/auth/totp/disable")
    async def api_totp_disable(request: Request):
        uid, err = _require_user(request)
        if err:
            return err
        body = await request.json()
        password = body.get("password") or ""
        code = (body.get("code") or "").strip()
        token = request.cookies.get(auth.SESSION_COOKIE)

        def work():
            c = conn()
            try:
                resolved = clientip.resolved(request)
                username = auth.username_for(c, uid)
                t = limiter.verdict(resolved, username, scope="reauth")
                if not t.allowed:
                    return ("blocked", t)
                if not auth.totp_enabled(c, uid):
                    return ("not_enrolled",)
                ok = auth.verify_password(c, username, password) is not None
                if ok:
                    ok = auth.verify_totp(
                        c, uid, code, skew=settings.security.totp_skew_steps
                    ) or auth.consume_recovery_code(c, uid, code)
                if not ok:
                    limiter.record_failure(resolved, username, scope="reauth")
                    auth.record_attempt(
                        c,
                        event="security_change",
                        outcome="failure",
                        reason="reauth_failed",
                        username=username,
                        user_id=uid,
                        ip=resolved.ip,
                        user_agent=clientip.user_agent(request),
                    )
                    return ("rejected",)
                limiter.record_success(resolved, username, scope="reauth")
                auth.disable_totp(c, uid)
                auth.delete_user_sessions(c, uid, except_token=token)
                auth.record_attempt(
                    c,
                    event="security_change",
                    outcome="success",
                    reason="totp_disabled",
                    username=username,
                    user_id=uid,
                    ip=resolved.ip,
                    user_agent=clientip.user_agent(request),
                )
                return ("ok",)
            finally:
                c.close()

        result = await run_in_threadpool(work)
        kind = result[0]
        if kind == "blocked":
            return _throttled(result[1])
        if kind == "not_enrolled":
            return JSONResponse({"error": "two-step sign-in is not enrolled"}, status_code=409)
        if kind == "rejected":
            return JSONResponse({"error": "Password or code was not accepted."}, status_code=403)
        _drop_cached_auth(request)
        return {"ok": True}

    @app.post("/api/auth/totp/recovery-codes")
    async def api_totp_recovery_codes(request: Request):
        uid, err = _require_user(request)
        if err:
            return err
        body = await request.json()
        password = body.get("password") or ""
        code = (body.get("code") or "").strip()
        token = request.cookies.get(auth.SESSION_COOKIE)

        def work():
            c = conn()
            try:
                resolved = clientip.resolved(request)
                username = auth.username_for(c, uid)
                t = limiter.verdict(resolved, username, scope="reauth")
                if not t.allowed:
                    return ("blocked", t)
                if not auth.totp_enabled(c, uid):
                    return ("not_enrolled",)
                ok = auth.verify_password(c, username, password) is not None
                if ok:
                    ok = auth.verify_totp(
                        c, uid, code, skew=settings.security.totp_skew_steps
                    ) or auth.consume_recovery_code(c, uid, code)
                if not ok:
                    limiter.record_failure(resolved, username, scope="reauth")
                    auth.record_attempt(
                        c,
                        event="security_change",
                        outcome="failure",
                        reason="reauth_failed",
                        username=username,
                        user_id=uid,
                        ip=resolved.ip,
                        user_agent=clientip.user_agent(request),
                    )
                    return ("rejected",)
                limiter.record_success(resolved, username, scope="reauth")
                codes = auth.generate_recovery_codes(
                    c, uid, count=settings.security.recovery_code_count
                )
                auth.delete_user_sessions(c, uid, except_token=token)
                auth.record_attempt(
                    c,
                    event="security_change",
                    outcome="success",
                    reason="recovery_codes_regenerated",
                    username=username,
                    user_id=uid,
                    ip=resolved.ip,
                    user_agent=clientip.user_agent(request),
                )
                return ("ok", codes)
            finally:
                c.close()

        result = await run_in_threadpool(work)
        kind = result[0]
        if kind == "blocked":
            return _throttled(result[1])
        if kind == "not_enrolled":
            return JSONResponse({"error": "two-step sign-in is not enrolled"}, status_code=409)
        if kind == "rejected":
            return JSONResponse({"error": "Password or code was not accepted."}, status_code=403)
        _drop_cached_auth(request)
        _, codes = result
        return {"codes": codes}

    # -- /api/auth/session-pinning (routes 15-16) ---------------------------- #

    @app.get("/api/auth/session-pinning")
    def api_session_pinning_status(request: Request):
        uid, err = _require_user(request)
        if err:
            return err
        token = request.cookies.get(auth.SESSION_COOKIE)
        c = conn()
        try:
            mode = auth.get_pin_mode(c, uid)
            other = auth.count_user_sessions(c, uid, except_token=token)
        finally:
            c.close()
        client = clientip.client_facts(request)
        return {
            "mode": mode,
            "modes": list(auth.PIN_MODES),
            "session_pinned": mode != auth.PIN_OFF,
            "other_sessions": other,
            "observed": {
                "user_agent": client.user_agent,
                "device_key": clientip.device_key(client.user_agent),
                "ip": client.ip,
                "network": clientip.ip_prefix(client.ip),
            },
        }

    @app.post("/api/auth/session-pinning")
    async def api_session_pinning_set(request: Request):
        uid, err = _require_user(request)
        if err:
            return err
        body = await request.json()
        mode = body.get("mode")
        password = body.get("password") or ""
        code = (body.get("code") or "").strip()
        if mode not in auth.PIN_MODES:
            return JSONResponse({"error": "unknown session-pinning mode"}, status_code=422)
        if not password:
            return JSONResponse({"error": "password is required"}, status_code=422)
        token = request.cookies.get(auth.SESSION_COOKIE)

        def work():
            c = conn()
            try:
                resolved = clientip.resolved(request)
                username = auth.username_for(c, uid)
                t = limiter.verdict(resolved, username, scope="reauth")
                if not t.allowed:
                    return ("blocked", t)
                ok = auth.verify_password(c, username, password) is not None
                if ok and auth.totp_enabled(c, uid):
                    ok = auth.verify_totp(
                        c, uid, code, skew=settings.security.totp_skew_steps
                    ) or auth.consume_recovery_code(c, uid, code)
                if not ok:
                    limiter.record_failure(resolved, username, scope="reauth")
                    auth.record_attempt(
                        c,
                        event="security_change",
                        outcome="failure",
                        reason="reauth_failed",
                        username=username,
                        user_id=uid,
                        ip=resolved.ip,
                        user_agent=clientip.user_agent(request),
                    )
                    return ("rejected",)
                limiter.record_success(resolved, username, scope="reauth")
                client = clientip.client_facts(request)
                # set_pin_mode IS the revocation for a pin change: it re-pins
                # `token` in place and deletes every other session, returning
                # that count. Calling delete_user_sessions here too would
                # double-count (section 5.1, R5).
                signed_out = auth.set_pin_mode(c, uid, mode, keep_token=token, client=client)
                auth.record_attempt(
                    c,
                    event="security_change",
                    outcome="success",
                    reason="session_pin_changed",
                    username=username,
                    user_id=uid,
                    ip=resolved.ip,
                    user_agent=clientip.user_agent(request),
                )
                return ("ok", signed_out)
            finally:
                c.close()

        result = await run_in_threadpool(work)
        kind = result[0]
        if kind == "blocked":
            return _throttled(result[1])
        if kind == "rejected":
            return JSONResponse({"error": "Password or code was not accepted."}, status_code=403)
        # set_pin_mode just deleted sessions this process may still have a
        # cached verdict for; without this they keep working for up to
        # _AUTH_CACHE_TTL (R5).
        _drop_cached_auth(request)
        _, signed_out = result
        return {"ok": True, "mode": mode, "signed_out_others": signed_out}

    # -- /api/security/* (routes 17-20) -------------------------------------- #

    @app.get("/api/security/access")
    def api_security_access(request: Request):
        _, err = _require_user(request)
        if err:
            return err
        resolved = clientip.resolved(request)
        trusts_loopback = any(
            clientip.is_loopback(str(net.network_address)) for net in trusted
        )
        shared_bucket = (
            resolved.peer_trusted
            and resolved.source == "socket_peer"
            and clientip.is_loopback(resolved.ip)
        )
        described = allowlist.describe()
        if described["entries"]:
            bits = list(described["entries"])
            if described["allow_private"]:
                bits.append("every local network")
            summary = "Currently allowed: " + ", ".join(bits) + ", and this machine (localhost)."
        else:
            summary = "Every address is allowed."
        trusted_str = [str(n) for n in trusted]
        return {
            "client_ip": resolved.ip,
            "effective_source": resolved.source,
            "peer": resolved.peer,
            "allowed": allowlist.allows(resolved.ip),
            "allowlist": described,
            "effective": {
                "summary": summary,
                "list_adds_nothing": allowlist.adds_nothing(),
            },
            "proxy": {
                "trusted_proxies": trusted_str,
                "peer_is_trusted_proxy": resolved.peer_trusted,
                "forwarded_for_present": bool(resolved.forwarded_for),
                "forwarded_for_raw": resolved.forwarded_for,
                "trusts_loopback": trusts_loopback,
                "shared_bucket": shared_bucket,
            },
            "in_effect": {
                "allow_from": list(settings.security.allow_from),
                "allow_private_networks": settings.security.allow_private_networks,
                "trusted_proxies": trusted_str,
            },
        }

    @app.get("/api/security/log")
    def api_security_log(
        request: Request,
        limit: int = 50,
        offset: int = 0,
        outcome: str | None = None,
        event: str | None = None,
        username: str | None = None,
        ip: str | None = None,
        since: int | None = None,
    ):
        _, err = _require_user(request)
        if err:
            return err
        if outcome is not None and outcome not in auth.AUTH_OUTCOMES:
            return JSONResponse({"error": "unknown outcome"}, status_code=422)
        if event is not None and event not in auth.AUTH_EVENTS:
            return JSONResponse({"error": "unknown event"}, status_code=422)
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        since_ts = int(since) if since is not None else 0
        c = conn()
        try:
            items, total = auth.auth_log(
                c,
                limit=limit,
                offset=offset,
                outcome=outcome,
                event=event,
                username=username,
                ip=ip,
                since=since,
            )
            summary = auth.log_summary(c, since_ts)
        finally:
            c.close()
        retention = auth.retention_policy()
        return {
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "best_effort": True,
            "summary": {**summary, "since": since_ts},
            "retention": {
                "days": retention["days"],
                "max_rows": retention["max_rows"],
                "enabled": retention["enabled"],
            },
        }

    @app.get("/api/security/throttles")
    def api_security_throttles(request: Request):
        _, err = _require_user(request)
        if err:
            return err
        return limiter.snapshot()

    @app.post("/api/security/throttles/clear")
    async def api_security_throttles_clear(request: Request):
        _, err = _require_user(request)
        if err:
            return err
        body = await request.json()
        if not isinstance(body, dict):
            return JSONResponse({"error": "body must be an object"}, status_code=422)
        ip = body.get("ip")
        username = body.get("username")

        def work():
            return limiter.clear(ip=ip, username=username)

        cleared = await run_in_threadpool(work)
        return {"ok": True, "cleared": cleared}
