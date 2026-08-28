"""Web GUI authentication: users, sessions, API keys, login rate limiting.

Stdlib-only and framework-free by design: every function operates over a
Connection (the same one the rest of the app uses) so the FastAPI layer that
lands later can wire these in without this module knowing anything about
HTTP. Secrets are never stored in plaintext -- passwords are scrypt-hashed with
a per-user salt, session tokens and API keys are stored as their sha256 hash,
and comparisons that matter use hmac.compare_digest to stay constant-time.

This package replaces the former single-file ``web/auth.py``. Every public
name below re-exports unchanged, so ``from synopticon.web import auth`` and
every ``auth.X`` call site keep working with no edit.
"""

from __future__ import annotations

from . import sessions, throttle
from .hashing import _scrypt, _sha256_hex
from .keys import create_api_key, list_api_keys, revoke_api_key, validate_api_key
from .sessions import (
    SESSION_COOKIE,
    create_session,
    delete_session,
    delete_user_sessions,
    purge_expired,
    validate_session,
)
from .throttle import LoginRateLimiter, Throttle
from .users import UsernameTakenError, change_password, create_user, has_users, list_users, username_for, verify_password

__all__ = [
    "sessions",
    "throttle",
    # hashing (private, but tests forge a token_hash with these directly)
    "_scrypt",
    "_sha256_hex",
    # users
    "UsernameTakenError",
    "create_user",
    "verify_password",
    "has_users",
    "list_users",
    "change_password",
    "username_for",
    # sessions
    "SESSION_COOKIE",
    "create_session",
    "validate_session",
    "delete_session",
    "delete_user_sessions",
    "purge_expired",
    # API keys
    "create_api_key",
    "validate_api_key",
    "revoke_api_key",
    "list_api_keys",
    # login throttling
    "Throttle",
    "LoginRateLimiter",
]
