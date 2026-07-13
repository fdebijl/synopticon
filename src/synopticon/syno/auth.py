"""SYNO.API.Auth login/logout.

`SynoClient` calls `login()` lazily on first `call()` (when `settings.nas.account`
is set) and again after a session-expiry error code. Device id (`did`) is
persisted in `sync_state` so a 2FA device stays trusted across process
restarts (avoids re-prompting `otp_code` on every run).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from synopticon.config import Settings
from synopticon.db import store
from synopticon.syno.client import SynoApiError, encode_params

if TYPE_CHECKING:
    from synopticon.syno.client import SynoClient

AUTH_API = "SYNO.API.Auth"
AUTH_DID_KEY = "auth_did"
_DEFAULT_AUTH_PATH = "auth.cgi"

# Documented SYNO.API.Auth error codes -> what the user should actually do.
_AUTH_ERROR_HINTS = {
    400: "Invalid account or password. Check nas.account / nas.password "
         "(env: SYNOPTICON_NAS__ACCOUNT / SYNOPTICON_NAS__PASSWORD).",
    401: "This account is disabled on the NAS. Enable it in DSM > Control Panel > User & Group.",
    402: "Permission denied: the account exists but may not access this service. "
         "Give it access to Synology Photos in DSM > Control Panel > User & Group > Applications.",
    403: "Two-factor authentication is required. Set nas.otp_code "
         "(env: SYNOPTICON_NAS__OTP_CODE) to the current 6-digit code and re-run; "
         "after one successful login the device token is stored and the code is no longer needed.",
    404: "The 2FA code was rejected. Codes expire after ~30s — set a fresh one in "
         "nas.otp_code (env: SYNOPTICON_NAS__OTP_CODE) and re-run immediately.",
    406: "The NAS enforces 2FA for this account, but none is configured. "
         "Set up 2FA for the account in DSM first, then supply nas.otp_code.",
    407: "This IP address is blocked by the NAS (too many failed logins / auto-block). "
         "Unblock it in DSM > Control Panel > Security > Protection > Allow/Block List.",
    408: "The account's password has expired and must be changed in DSM before logging in.",
    409: "The account's password has expired and must be changed in DSM before logging in.",
    410: "The account's password must be changed (admin-enforced) in DSM before logging in.",
}


class SynoAuthError(SynoApiError):
    """Login failed, with an actionable explanation when the code is known."""

    def __init__(self, code: int | None, account: str):
        super().__init__(code, AUTH_API, "login")
        hint = _AUTH_ERROR_HINTS.get(
            code or 0,
            "Unrecognized auth error code — check the NAS URL points at DSM 7 "
            "(and see https://kb.synology.com for SYNO.API.Auth error codes).",
        )
        self.hint = hint
        # Overwrite the generic message with the actionable one.
        self.args = (
            f"Synology login failed for account '{account}' (error {code}): {hint}",
        )


@dataclass
class AuthSession:
    sid: str
    synotoken: str
    device_id: str | None = None


def _auth_path(client: "SynoClient") -> str:
    info = client.api_info.get(AUTH_API) or {}
    return info.get("path", _DEFAULT_AUTH_PATH)


def login(client: "SynoClient", settings: Settings, conn: sqlite3.Connection) -> AuthSession:
    """POST SYNO.API.Auth login; returns an AuthSession (sid + synotoken + did)."""
    version = client.version_for(AUTH_API, 3)
    did = store.get_state(conn, AUTH_DID_KEY)

    params: dict[str, Any] = {
        "api": AUTH_API,
        "method": "login",
        "version": version,
        "account": settings.nas.account,
        "passwd": settings.nas.password.get_secret_value(),
        "enable_syno_token": "yes",
        "format": "sid",
        "enable_device_token": "yes",
        "device_name": settings.nas.device_name,
    }
    if did:
        params["device_id"] = did
    if settings.nas.otp_code:
        params["otp_code"] = settings.nas.otp_code

    payload = encode_params(params)
    path = _auth_path(client)
    resp = client._send("POST", f"/webapi/{path}", data=payload)
    envelope = resp.json()
    if not envelope.get("success", False):
        code = (envelope.get("error") or {}).get("code")
        raise SynoAuthError(code, settings.nas.account)

    data = envelope.get("data") or {}
    new_did = data.get("did") or did
    if new_did and new_did != did:
        store.set_state(conn, AUTH_DID_KEY, new_did)

    return AuthSession(
        sid=data["sid"],
        synotoken=data.get("synotoken", ""),
        device_id=new_did,
    )


def logout(client: "SynoClient", settings: Settings, session: AuthSession) -> None:
    """Best-effort SYNO.API.Auth logout; does not raise on failure."""
    version = client.version_for(AUTH_API, 1)
    path = _auth_path(client)
    payload = encode_params({"api": AUTH_API, "method": "logout", "version": version})
    resp = client._send("POST", f"/webapi/{path}", data=payload)
    try:
        resp.json()
    except ValueError:
        pass
