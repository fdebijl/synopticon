"""RFC 6238 TOTP: pure stdlib, no `Connection`, no HTTP.

Framework-free by design (§4.4, W1): `web/auth/twofactor.py` is the only
caller, and it stores/reads the secret, the replay guard and everything else
stateful. This module has no state of its own and knows nothing about a user
or a database row -- it only turns a secret and a time step into a code, and
back.

Only SHA1 is offered, matching `provisioning_uri`'s `algorithm=SHA1` -- every
authenticator app in general use implements the RFC 6238 default and nothing
here needs a second algorithm to support.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from urllib.parse import quote

#: RFC 6238 default step length, in seconds.
_PERIOD = 30

#: RFC 6238 / Google Authenticator default code length.
_DIGITS = 6


def generate_secret(nbytes: int = 20) -> str:
    """A fresh CSPRNG secret, base32-encoded, unpadded.

    20 bytes (160 bits) matches the RFC 6238 test-vector key length and is what
    every mainstream authenticator app expects.
    """
    return base64.b32encode(secrets.token_bytes(nbytes)).decode("ascii").rstrip("=")


def normalize_code(raw: str) -> str:
    """Strip everything but ASCII digits: whitespace, hyphens, anything a phone
    keyboard or a copy-paste might add around a 6-digit code.

    ASCII specifically, not ``str.isdigit`` -- that is true for Arabic-Indic and
    Devanagari digits too, and `hmac.compare_digest` raises TypeError on a
    non-ASCII str, which would turn an unauthenticated code submission into a
    500 that bypasses both the throttle and the sign-in log.
    """
    return "".join(ch for ch in (raw or "") if ch in "0123456789")


def current_step(now: float | None = None, period: int = _PERIOD) -> int:
    """The RFC 6238 time step for `now` (default: wall clock)."""
    ts = time.time() if now is None else now
    return int(ts // period)


def _decode_secret(secret: str) -> bytes:
    padded = secret.strip().upper()
    padded += "=" * (-len(padded) % 8)
    return base64.b32decode(padded)


def code_for(secret: str, step: int, digits: int = _DIGITS) -> str:
    """The RFC 4226 HOTP value for `secret` at time step `step`, zero-padded
    to `digits` characters."""
    key = _decode_secret(secret)
    msg = step.to_bytes(8, "big")
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    truncated = int.from_bytes(digest[offset : offset + 4], "big") & 0x7FFFFFFF
    return str(truncated % (10**digits)).zfill(digits)


def verify(
    secret: str,
    code: str,
    *,
    now: float | None = None,
    skew: int = 1,
    digits: int = _DIGITS,
    period: int = _PERIOD,
) -> int | None:
    """The matched time step, or None.

    Tries every step within `skew` of `current_step(now, period)` -- never
    fewer, so a phone a few seconds fast or slow still works, and never more,
    so the search window stays the one `twofactor.verify_totp`'s replay guard
    (and its `_CLOCK_REWIND_STEPS` escape) is built around. Every candidate is
    compared with `hmac.compare_digest`, and the loop runs to completion
    without an early `break` on a match, so which offset (if any) matched
    carries no timing signal.
    """
    normalized = normalize_code(code)
    if len(normalized) != digits or not normalized.isdigit():
        return None
    base_step = current_step(now, period)
    matched: int | None = None
    for delta in range(-skew, skew + 1):
        step = base_step + delta
        if step < 0:
            continue
        candidate = code_for(secret, step, digits=digits)
        if hmac.compare_digest(candidate, normalized):
            matched = step
    return matched


def provisioning_uri(secret: str, *, account: str, issuer: str) -> str:
    """`otpauth://totp/<issuer>:<account>?secret=...` for an authenticator app.

    Every component is percent-encoded on its own -- the label's ':' separator
    and the query's '&'/'=' stay literal, which is what lets an authenticator
    app parse the URI at all.
    """
    label = f"{quote(issuer, safe='')}:{quote(account, safe='')}"
    params = (
        ("secret", secret),
        ("issuer", issuer),
        ("algorithm", "SHA1"),
        ("digits", str(_DIGITS)),
        ("period", str(_PERIOD)),
    )
    query = "&".join(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in params)
    return f"otpauth://totp/{label}?{query}"
