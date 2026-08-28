"""Login throttling (SEC5). Owner: W5.

Two tiers, both always armed (D5, D6): per-(scope, address prefix, username)
and per-(address prefix). There is no global tier and no exemption of any
kind -- see web/clientip.py RULE 2.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

from .. import clientip

_SWEEP_INTERVAL = 60.0

# A pair key is always a 3-tuple (scope, ip_prefix, username); an address
# window entry tagged with this sentinel therefore can never match one, which
# is what keeps a peek-miss charge (record_address) from ever being refunded.
_UNREFUNDABLE = None


@dataclass(frozen=True)
class Throttle:
    allowed: bool
    retry_after: int  # whole seconds, for the Retry-After header; 0 when allowed
    tier: str  # "" | "pair" | "ip" -- NEVER sent to a client


@dataclass
class _PairEntry:
    """One (scope, ip_prefix, username) pair. Two independent blocks share the
    same bookkeeping row: `backoff_until` is today's exponential brake on
    definitive failures, `attempt_block_until` is the sliding-window brake on
    pending-or-failed attempts. `verdict` reports whichever is later -- "the
    LONGER of (a) and (b)"."""

    attempts: "deque[float]"
    failures: int = 0
    backoff_until: float = 0.0
    attempt_block_until: float = 0.0
    forget_at: float = 0.0


@dataclass
class _AddressEntry:
    """One address prefix. `window` holds `(timestamp, pair_key)` so a success
    can refund exactly the entries its own pair charged (F1) -- entries tagged
    `_UNREFUNDABLE` (a peek-miss, no pair) can never be refunded by anyone."""

    window: "deque[tuple[float, tuple[str, str, str] | None]]"
    block_until: float = 0.0
    forget_at: float = 0.0


class LoginRateLimiter:
    """In-memory login throttle: two tiers, always armed, no exemption.

    Purely in-memory (a lockout should not survive a restart, and it is a soft
    DoS guard, not an audit record). The clock is injectable so tests need no
    sleeps.
    """

    def __init__(
        self,
        base_seconds: float = 2.0,
        cap_seconds: float = 300.0,
        forget_seconds: float = 900.0,
        pair_max_attempts: int = 10,
        pair_window_seconds: float = 300.0,
        pair_block_seconds: float = 300.0,
        ip_max_failures: int = 20,
        ip_window_seconds: float = 300.0,
        ip_block_seconds: float = 300.0,
        max_tracked: int = 4096,
        clock: Callable[[], float] | None = None,
    ) -> None:
        # Raises ValueError if forget_seconds <= cap_seconds: decay must never
        # be able to shorten a live lockout.
        if forget_seconds <= cap_seconds:
            raise ValueError("forget_seconds must be greater than cap_seconds")
        self._base = base_seconds
        self._cap = cap_seconds
        self._forget_seconds = forget_seconds
        self._pair_max_attempts = pair_max_attempts
        self._pair_window_seconds = pair_window_seconds
        self._pair_block_seconds = pair_block_seconds
        self._ip_max_failures = ip_max_failures
        self._ip_window_seconds = ip_window_seconds
        self._ip_block_seconds = ip_block_seconds
        self._max_tracked = max_tracked
        self._clock = clock or time.monotonic
        # THERE IS NO `shared_networks`, NO `allow_private`, NO
        # `known_ip_seconds` and NO `global_*` KWARG, and there never may be
        # one (D5, D6).
        self._pairs: dict[tuple[str, str, str], _PairEntry] = {}
        self._addresses: dict[str, _AddressEntry] = {}
        self._last_sweep = 0.0
        # Every public method takes this for its whole body, and no body
        # performs I/O (section 4.0, D8).
        self._lock = threading.Lock()

    # -- keying --------------------------------------------------------- #

    @staticmethod
    def _ip_of(client: Any) -> str:
        """`client` is a clientip.ResolvedClient, or a bare address string.

        Under D5 this is not a security-relevant choice at all: nothing in
        this file reads any field of `client` except `.ip`. There is no
        exemption a degraded record could fall into.
        """
        if isinstance(client, str):
            return client
        return client.ip

    @staticmethod
    def _truncate_username(username: str) -> str:
        return (username or "")[: clientip.USERNAME_MAX]

    def _pair_key(self, ip: str, username: str, scope: str) -> tuple[str, str, str]:
        return (scope, clientip.ip_prefix(ip), self._truncate_username(username))

    # -- entry lookup, with the per-touch expiry drop -------------------- #

    def _get_pair(self, key: tuple[str, str, str], now: float, create: bool) -> _PairEntry | None:
        entry = self._pairs.get(key)
        if entry is not None and entry.forget_at <= now:
            del self._pairs[key]
            entry = None
        if entry is None and create:
            entry = _PairEntry(attempts=deque(maxlen=self._pair_max_attempts or None))
            self._pairs[key] = entry
        return entry

    def _get_address(self, addr_key: str, now: float, create: bool) -> _AddressEntry | None:
        entry = self._addresses.get(addr_key)
        if entry is not None and entry.forget_at <= now:
            del self._addresses[addr_key]
            entry = None
        if entry is None and create:
            entry = _AddressEntry(window=deque(maxlen=self._ip_max_failures or None))
            self._addresses[addr_key] = entry
        return entry

    # -- charging --------------------------------------------------------- #

    def _charge_address(self, ip: str, pair_key: tuple[str, str, str] | None, now: float) -> None:
        addr_key = clientip.ip_prefix(ip)
        addr = self._get_address(addr_key, now, create=True)
        assert addr is not None
        addr.window.append((now, pair_key))
        if len(addr.window) >= self._ip_max_failures and (now - addr.window[0][0]) <= self._ip_window_seconds:
            addr.block_until = now + self._ip_block_seconds
        # FORGET DEADLINES ARE PER ENTRY KIND (F2): an address entry outlives
        # a pair entry's flat 900s so a block above fifteen minutes cannot
        # evaporate from the bookkeeping being swept while the block is live.
        addr.forget_at = now + max(self._forget_seconds, self._ip_block_seconds + self._ip_window_seconds)

    # -- verdict composition ----------------------------------------------- #

    @staticmethod
    def _pair_verdict(entry: _PairEntry | None, now: float) -> tuple[bool, float]:
        if entry is None:
            return False, 0.0
        until = max(entry.backoff_until, entry.attempt_block_until)
        return now < until, until

    def _address_verdict(self, addr_key: str, now: float) -> tuple[bool, float]:
        entry = self._get_address(addr_key, now, create=False)
        if entry is None:
            return False, 0.0
        return now < entry.block_until, entry.block_until

    @staticmethod
    def _retry_after(until: float, now: float) -> int:
        return max(1, math.ceil(until - now))

    def _combined_verdict(self, pair_entry: _PairEntry | None, addr_key: str, now: float) -> Throttle:
        blocked, until = self._pair_verdict(pair_entry, now)
        if blocked:
            return Throttle(False, self._retry_after(until, now), "pair")
        blocked, until = self._address_verdict(addr_key, now)
        if blocked:
            return Throttle(False, self._retry_after(until, now), "ip")
        return Throttle(True, 0, "")

    # -- memory bound ------------------------------------------------------- #

    def _maybe_sweep(self, now: float) -> None:
        if now - self._last_sweep >= _SWEEP_INTERVAL:
            self._sweep(now)

    def _sweep(self, now: float) -> None:
        for key in [k for k, e in self._pairs.items() if e.forget_at <= now]:
            del self._pairs[key]
        for key in [k for k, e in self._addresses.items() if e.forget_at <= now]:
            del self._addresses[key]
        while len(self._pairs) + len(self._addresses) > self._max_tracked:
            oldest_pair = min(self._pairs.items(), key=lambda kv: kv[1].forget_at, default=None)
            oldest_addr = min(self._addresses.items(), key=lambda kv: kv[1].forget_at, default=None)
            if oldest_pair is None and oldest_addr is None:
                break
            if oldest_addr is None or (oldest_pair is not None and oldest_pair[1].forget_at <= oldest_addr[1].forget_at):
                del self._pairs[oldest_pair[0]]  # type: ignore[index]
            else:
                del self._addresses[oldest_addr[0]]  # type: ignore[index]
        self._last_sweep = now

    # -- public API ----------------------------------------------------- #

    def check(self, ip: str, username: str, scope: str = "password") -> bool:
        """True if a login attempt is currently allowed for this (ip, username).

        Unchanged signature and meaning: `verdict(...).allowed`. The `scope`
        default is what keeps the existing tests calling it two-positionally.
        """
        return self.verdict(ip, username, scope).allowed

    def verdict(self, client: Any, username: str, scope: str = "password") -> Throttle:
        """Both tiers, always (D5, D6):

          pair - per (scope, ip_prefix, username). The LONGER of
                 (a) the exponential backoff on definitive failures and
                 (b) a sliding window of `pair_max_attempts` attempts
                     (pending or failed) in `pair_window_seconds`, blocking
                     for `pair_block_seconds`.
          ip   - sliding window of attempts from this address prefix, any
                 username, blocking for `ip_block_seconds`.

        THERE IS NO TRUST PARAMETER AND THERE NEVER MAY BE ONE. `client` may
        be a clientip.ResolvedClient or a bare address string; only `.ip` is
        ever read. Do not add a keyword, a ResolvedClient field read, or an
        `if is_loopback(...)` short-circuit -- clientip.py's RULE 2 records
        three separate revisions in which exactly that disabled login
        throttling for the entire internet.
        """
        ip = self._ip_of(client)
        now = self._clock()
        with self._lock:
            self._maybe_sweep(now)
            pair_key = self._pair_key(ip, username, scope)
            pair_entry = self._get_pair(pair_key, now, create=False)
            addr_key = clientip.ip_prefix(ip)
            return self._combined_verdict(pair_entry, addr_key, now)

    def verdict_address(self, client: Any, scope: str = "password") -> Throttle:
        """The `ip` tier ALONE (D9c), for a caller that does not yet know the
        username and must not buy a connection to find out. Its only caller is
        POST /api/auth/login/verify, immediately before hop A. It never
        consults or creates a pair entry, so it cannot leak whether a username
        exists. `tier` is "" or "ip".
        """
        ip = self._ip_of(client)
        now = self._clock()
        with self._lock:
            self._maybe_sweep(now)
            addr_key = clientip.ip_prefix(ip)
            blocked, until = self._address_verdict(addr_key, now)
            if blocked:
                return Throttle(False, self._retry_after(until, now), "ip")
            return Throttle(True, 0, "")

    def record_pending(self, client: Any, username: str, scope: str = "password") -> None:
        """An attempt whose verdict is not yet known -- the SEC1/SEC5 hinge.
        Feeds the outcome-independent per-pair ATTEMPT window and the address
        window; never arms the exponential pair backoff. The address-window
        entry it appends carries this pair's key (see `record_success`).
        """
        ip = self._ip_of(client)
        now = self._clock()
        with self._lock:
            self._maybe_sweep(now)
            pair_key = self._pair_key(ip, username, scope)
            if self._pair_max_attempts > 0:
                entry = self._get_pair(pair_key, now, create=True)
                assert entry is not None
                entry.attempts.append(now)
                if (
                    len(entry.attempts) >= self._pair_max_attempts
                    and (now - entry.attempts[0]) <= self._pair_window_seconds
                ):
                    entry.attempt_block_until = now + self._pair_block_seconds
                entry.forget_at = now + self._forget_seconds
            if self._ip_max_failures > 0:
                self._charge_address(ip, pair_key, now)

    def record_address(self, client: Any, scope: str = "password") -> None:
        """Charges the ADDRESS window only (D9c) -- no pair key, no pair entry.
        Its only caller is step two's peek-miss path, which names no account
        and can therefore never be refunded by anyone's success. Tagged
        `_UNREFUNDABLE` so it decays with the window and nothing gives it back.
        """
        ip = self._ip_of(client)
        now = self._clock()
        with self._lock:
            self._maybe_sweep(now)
            if self._ip_max_failures > 0:
                self._charge_address(ip, _UNREFUNDABLE, now)

    def record_failure(self, client: Any, username: str, scope: str = "password") -> Throttle:
        """A definitive failure: arms the exponential pair backoff AND feeds
        both windows (pair attempt, address), tagging the address entry with
        this pair's key.
        """
        ip = self._ip_of(client)
        now = self._clock()
        with self._lock:
            self._maybe_sweep(now)
            pair_key = self._pair_key(ip, username, scope)
            entry = self._get_pair(pair_key, now, create=True)
            assert entry is not None
            entry.failures += 1
            delay = min(self._base * (2 ** (entry.failures - 1)), self._cap)
            entry.backoff_until = now + delay
            if self._pair_max_attempts > 0:
                entry.attempts.append(now)
                if (
                    len(entry.attempts) >= self._pair_max_attempts
                    and (now - entry.attempts[0]) <= self._pair_window_seconds
                ):
                    entry.attempt_block_until = now + self._pair_block_seconds
            entry.forget_at = now + self._forget_seconds
            if self._ip_max_failures > 0:
                self._charge_address(ip, pair_key, now)
            addr_key = clientip.ip_prefix(ip)
            return self._combined_verdict(entry, addr_key, now)

    def record_success(self, client: Any, username: str, scope: str = "password") -> None:
        """Clears the pair backoff entry AND the pair attempt window for this
        (scope, ip_prefix, username), and REFUNDS the address window.

        THE REFUND IS PAIR-EXACT (F1): the address window is rebuilt without
        the entries whose pair_key equals this pair's key, never "the most
        recent N" -- an untagged refund behind a shared address would pop
        whoever charged them, not necessarily this pair.
        """
        ip = self._ip_of(client)
        now = self._clock()
        with self._lock:
            self._maybe_sweep(now)
            pair_key = self._pair_key(ip, username, scope)
            self._pairs.pop(pair_key, None)
            addr_key = clientip.ip_prefix(ip)
            addr = self._addresses.get(addr_key)
            if addr is not None:
                kept = [(t, k) for t, k in addr.window if k != pair_key]
                addr.window = deque(kept, maxlen=self._ip_max_failures or None)

    def snapshot(self) -> dict[str, Any]:
        """THE shape route 19 returns verbatim -- no key is added or renamed.
        There is no "global" key, no "known_ips" and no "shared_networks": the
        tier, the set and the exemption they described are all deleted (D5,
        D6). Built entirely inside `self._lock` and returns plain data, never
        a view onto internal state.
        """
        now = self._clock()
        with self._lock:
            self._maybe_sweep(now)
            pairs = []
            for (scope, ip, username), entry in self._pairs.items():
                blocked, until = self._pair_verdict(entry, now)
                pairs.append(
                    {
                        "scope": scope,
                        "ip": ip,
                        "username": username,
                        "failures": entry.failures,
                        "attempts": len(entry.attempts),
                        "locked_for": self._retry_after(until, now) if blocked else 0,
                        "forget_in": max(0, int(entry.forget_at - now)),
                    }
                )
            pairs.sort(key=lambda p: p["locked_for"], reverse=True)  # locked first
            ips = []
            for addr_key, entry in self._addresses.items():
                blocked, until = self._address_verdict(addr_key, now)
                ips.append(
                    {
                        "ip": addr_key,
                        "failures_in_window": len(entry.window),
                        "blocked_for": self._retry_after(until, now) if blocked else 0,
                    }
                )
            ips.sort(key=lambda p: p["blocked_for"], reverse=True)
            return {
                "pairs": pairs,
                "ips": ips,
                "tracked": len(self._pairs) + len(self._addresses),
                "max_tracked": self._max_tracked,
                "thresholds": {
                    "pair_max_attempts": self._pair_max_attempts,
                    "ip_max_failures": self._ip_max_failures,
                },
            }

    def clear(self, ip: str | None = None, username: str | None = None) -> int:
        """Drop matching pair entries (backoff and attempt window) plus
        matching address-window entries; `ip` is normalised through
        clientip.ip_prefix and `username` through the same truncation the key
        builder applies. Returns how many went. No args clears everything.
        """
        with self._lock:
            norm_ip = clientip.ip_prefix(ip) if ip is not None else None
            norm_user = self._truncate_username(username) if username is not None else None
            if norm_ip is None and norm_user is None:
                removed = len(self._pairs) + len(self._addresses)
                self._pairs.clear()
                self._addresses.clear()
                return removed
            removed = 0
            for key in [
                k
                for k in self._pairs
                if (norm_ip is None or k[1] == norm_ip) and (norm_user is None or k[2] == norm_user)
            ]:
                del self._pairs[key]
                removed += 1
            if norm_ip is not None and norm_ip in self._addresses:
                del self._addresses[norm_ip]
                removed += 1
            return removed
