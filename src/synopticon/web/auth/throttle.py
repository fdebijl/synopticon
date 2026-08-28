"""Login rate limiting: an in-memory backoff on failed sign-in attempts.

Deliberately not a table. A backoff should not survive a restart, and this is a
soft guard against guessing, not an audit record -- the sign-in log is where
evidence belongs.
"""

from __future__ import annotations

import time
from typing import Callable


class LoginRateLimiter:
    """In-memory per-(ip, username) exponential backoff on failed logins.

    After a failure, that (ip, username) pair is locked out for `base` seconds,
    doubling with each consecutive failure up to `cap`. A success resets the pair.
    Purely in-memory (backoff should not survive a restart, and it is a soft DoS
    guard, not an audit record). The clock is injectable so tests need no sleeps.
    """

    def __init__(
        self,
        base_seconds: float = 2.0,
        cap_seconds: float = 300.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._base = base_seconds
        self._cap = cap_seconds
        self._clock = clock or time.monotonic
        # (ip, username) -> [consecutive_failures, locked_until_ts]
        self._state: dict[tuple[str, str], list[float]] = {}

    def _key(self, ip: str, username: str) -> tuple[str, str]:
        return (ip, username)

    def check(self, ip: str, username: str) -> bool:
        """True if a login attempt is currently allowed for this (ip, username)."""
        entry = self._state.get(self._key(ip, username))
        if entry is None:
            return True
        return self._clock() >= entry[1]

    def record_failure(self, ip: str, username: str) -> None:
        """Register a failed attempt and (re)arm the backoff window."""
        key = self._key(ip, username)
        entry = self._state.get(key)
        failures = (int(entry[0]) if entry else 0) + 1
        delay = min(self._base * (2 ** (failures - 1)), self._cap)
        self._state[key] = [failures, self._clock() + delay]

    def record_success(self, ip: str, username: str) -> None:
        """Clear all backoff for this (ip, username) after a successful login."""
        self._state.pop(self._key(ip, username), None)
