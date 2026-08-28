"""web.auth.throttle: the two always-armed login-throttle tiers (SEC5).

Named tests here pin the corrections the security contract calls out
explicitly (D5, D6, D9c, F1, F2, D8) so a regression is loud. Purely
in-memory -- no database, no app, no clientip fixtures needed.
"""

from __future__ import annotations

import inspect
import threading
import time

import pytest

from synopticon.web import clientip
from synopticon.web.auth import throttle as throttle_mod
from synopticon.web.auth.throttle import LoginRateLimiter


class FakeClock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _resolved(ip: str, *, source="socket_peer", peer=None, forwarded_for="", peer_trusted=False):
    return clientip.ResolvedClient(
        ip=ip,
        source=source,
        peer=peer if peer is not None else ip,
        forwarded_for=forwarded_for,
        scheme="http",
        peer_trusted=peer_trusted,
    )


# -- D5: loopback is throttled like any other address, on every variant ----- #


def _loopback_variants():
    """Three records that describe the SAME connection differently -- an
    honest direct hit, one carrying a forged X-Forwarded-For nobody trusts,
    and one where the peer itself is a listed trusted proxy. D5 requires all
    three to throttle identically, because nothing here may read anything but
    `.ip`."""
    return [
        _resolved("127.0.0.1"),
        _resolved("127.0.0.1", forwarded_for="203.0.113.9", source="socket_peer"),
        _resolved("127.0.0.1", peer_trusted=True),
    ]


@pytest.mark.parametrize("client", _loopback_variants())
def test_loopback_blocked_by_pair_tier_at_threshold(client):
    # record_pending, not record_failure -- it never arms the exponential
    # backoff, so this isolates the sliding-window half of the pair tier.
    clock = FakeClock()
    rl = LoginRateLimiter(pair_max_attempts=3, ip_max_failures=1000, clock=clock)
    for _ in range(2):
        rl.record_pending(client, "admin")
    assert rl.verdict(client, "admin").allowed is True
    rl.record_pending(client, "admin")
    v = rl.verdict(client, "admin")
    assert v.allowed is False
    assert v.tier == "pair"


@pytest.mark.parametrize("client", _loopback_variants())
def test_loopback_blocked_by_address_tier_at_threshold(client):
    clock = FakeClock()
    rl = LoginRateLimiter(pair_max_attempts=1000, ip_max_failures=3, clock=clock)
    # a different username each time so the pair tier never fires -- only the
    # address tier can be responsible for the block below.
    for i in range(2):
        rl.record_failure(client, f"user{i}")
    assert rl.verdict(client, "someone-else").allowed is True
    rl.record_failure(client, "user2")
    v = rl.verdict(client, "someone-else")
    assert v.allowed is False
    assert v.tier == "ip"


def test_degraded_record_leaves_both_tiers_armed():
    """A route exercised without ProxyHeaders (a unit test, a sub-app) falls
    back to clientip.resolved()'s socket-peer-only record. Under D5 that
    degraded record has no special standing at all -- it throttles exactly
    like the bare string the four legacy tests already use, on both tiers."""
    scope = {"type": "http", "client": ("127.0.0.1", 4444), "scheme": "http", "headers": []}
    degraded = clientip.resolved(scope)

    for client in ("127.0.0.1", degraded):
        clock = FakeClock()
        rl = LoginRateLimiter(pair_max_attempts=2, ip_max_failures=1000, clock=clock)
        rl.record_pending(client, "admin")
        assert rl.verdict(client, "admin").allowed is True
        rl.record_pending(client, "admin")
        assert rl.verdict(client, "admin").allowed is False  # pair window tripped

    for client in ("127.0.0.1", degraded):
        clock = FakeClock()
        rl = LoginRateLimiter(pair_max_attempts=1000, ip_max_failures=1, clock=clock)
        rl.record_failure(client, "admin")
        assert rl.verdict(client, "someone-else").allowed is False  # address tripped


def test_no_field_but_ip_changes_the_verdict():
    clock = FakeClock()
    rl = LoginRateLimiter(pair_max_attempts=2, clock=clock)
    a = _resolved("198.51.100.5", source="socket_peer", peer_trusted=False, forwarded_for="")
    b = _resolved("198.51.100.5", source="forwarded", peer_trusted=True, forwarded_for="9.9.9.9")

    assert rl.verdict(a, "admin") == rl.verdict(b, "admin")
    rl.record_failure(a, "admin")
    # charged under `a`'s facts; read back under `b`'s -- identical either way,
    # because only `.ip` was ever consulted.
    assert rl.verdict(a, "admin") == rl.verdict(b, "admin")
    rl.record_failure(b, "admin")
    assert rl.verdict(a, "admin") == rl.verdict(b, "admin")


# -- D6: no global tier, no trust parameter, no exemption -------------------- #


def test_verdict_signature_has_no_parameter_beyond_scope():
    params = list(inspect.signature(LoginRateLimiter.verdict).parameters)
    assert params == ["self", "client", "username", "scope"]
    params = list(inspect.signature(LoginRateLimiter.verdict_address).parameters)
    assert params == ["self", "client", "scope"]


def test_snapshot_has_no_global_tier_keys():
    rl = LoginRateLimiter(clock=FakeClock())
    rl.record_failure("1.2.3.4", "admin")
    snap = rl.snapshot()
    assert "global" not in snap
    assert "known_ips" not in snap
    assert "shared_networks" not in snap
    assert set(snap) == {"pairs", "ips", "tracked", "max_tracked", "thresholds"}


def test_limiter_exposes_no_trust_kwargs():
    import pytest as _pytest

    for forbidden in ("shared_networks", "allow_private", "known_ip_seconds", "global_max_failures"):
        with _pytest.raises(TypeError):
            LoginRateLimiter(**{forbidden: 1})  # type: ignore[arg-type]


# -- F1: the address-window refund is pair-exact ----------------------------- #


def test_record_success_refunds_only_its_own_pair():
    clock = FakeClock()
    rl = LoginRateLimiter(pair_max_attempts=1000, ip_max_failures=2, clock=clock)
    ip = "203.0.113.1"
    rl.record_failure(ip, "alice")  # charges the address window once, tagged 'alice'
    rl.record_failure(ip, "bob")  # second charge, tagged 'bob' -- trips ip_max_failures=2

    assert rl.verdict(ip, "carol").tier == "ip"  # address tier is blocking everyone now

    rl.record_success(ip, "alice")  # refunds ONLY alice's charge

    snap = rl.snapshot()
    ip_row = next(r for r in snap["ips"] if r["ip"] == clientip.ip_prefix(ip))
    assert ip_row["failures_in_window"] == 1  # bob's charge survives


def test_twenty_completed_signins_from_one_prefix_do_not_trip_address_tier():
    clock = FakeClock()
    rl = LoginRateLimiter(clock=clock)  # ip_max_failures=20 default
    ip = "203.0.113.2"
    for i in range(20):
        rl.record_pending(ip, f"user{i}")
        rl.record_success(ip, f"user{i}")
    assert rl.verdict(ip, "final-check").allowed is True


# -- F2: an address entry's forget deadline outlives its own block ----------- #


def test_address_forget_deadline_exceeds_block_seconds():
    clock = FakeClock()
    rl = LoginRateLimiter(pair_max_attempts=0, ip_max_failures=1, ip_block_seconds=3600.0, clock=clock)
    ip = "203.0.113.3"
    rl.record_failure(ip, "admin")
    clock.advance(5.0)  # let the (unrelated) exponential backoff decay
    assert rl.verdict(ip, "admin").tier == "ip"

    clock.advance(895.0)  # 900s total idle -- forget_seconds' flat default,
    # the bug this guards against would have evicted the bookkeeping here
    v = rl.verdict(ip, "admin")
    assert v.allowed is False
    assert v.tier == "ip"


# -- threshold 0 disables a tier, never blocks on the first attempt --------- #


def test_zero_threshold_disables_pair_tier():
    clock = FakeClock()
    rl = LoginRateLimiter(pair_max_attempts=0, ip_max_failures=1000, clock=clock)
    ip = "203.0.113.4"
    for _ in range(50):
        rl.record_pending(ip, "admin")
    assert rl.verdict(ip, "admin").allowed is True


def test_zero_threshold_disables_address_tier():
    clock = FakeClock()
    rl = LoginRateLimiter(pair_max_attempts=1000, ip_max_failures=0, clock=clock)
    ip = "203.0.113.5"
    for i in range(50):
        rl.record_address(ip)
    assert rl.verdict_address(ip).allowed is True


# -- the pair attempt window trips at exactly N == pair_max_attempts -------- #


def test_pair_attempt_window_trips_at_exact_threshold():
    clock = FakeClock()
    rl = LoginRateLimiter(pair_max_attempts=5, clock=clock)
    client = _resolved("198.51.100.9")
    for _ in range(4):
        rl.record_pending(client, "admin")
    assert rl.verdict(client, "admin").allowed is True
    rl.record_pending(client, "admin")
    v = rl.verdict(client, "admin")
    assert v.allowed is False
    assert v.tier == "pair"


# -- D9c: record_address's sentinel charge is never refunded ---------------- #


def test_record_address_charge_is_never_refunded():
    clock = FakeClock()
    rl = LoginRateLimiter(ip_max_failures=1, clock=clock)
    ip = "203.0.113.6"
    rl.record_address(ip)  # a peek-miss: no username, no pair
    assert rl.verdict_address(ip).allowed is False

    rl.record_success(ip, "anyone-at-all")  # cannot name the sentinel's pair key
    assert rl.verdict_address(ip).allowed is False


# -- unbounded request input is bounded inside the mitigation ---------------- #


def test_huge_username_produces_a_bounded_key():
    clock = FakeClock()
    rl = LoginRateLimiter(clock=clock)
    huge = "x" * (1024 * 1024)
    rl.record_pending("203.0.113.7", huge)
    snap = rl.snapshot()
    row = next(r for r in snap["pairs"] if r["ip"] == clientip.ip_prefix("203.0.113.7"))
    assert len(row["username"]) == clientip.USERNAME_MAX


# -- D8: every public method is safe under concurrent access ---------------- #


def test_concurrent_access_raises_nothing():
    rl = LoginRateLimiter(pair_max_attempts=5, ip_max_failures=5, max_tracked=64)
    stop = threading.Event()
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        i = 0
        while not stop.is_set():
            try:
                rl.record_pending(f"10.0.{n}.1", f"user{i % 7}")
                rl.verdict(f"10.0.{n}.1", f"user{i % 7}")
                rl.snapshot()
                i += 1
            except BaseException as exc:  # noqa: BLE001 - a raise here IS the failure
                errors.append(exc)
                stop.set()

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(20)]
    for t in threads:
        t.start()
    time.sleep(2.0)
    stop.set()
    for t in threads:
        t.join()

    assert errors == []


def test_module_exposes_no_global_tier_or_exemption_surface():
    # D6: no server-wide tier ever existed here. D5: no exemption predicate.
    for name in ("global_max_failures", "known_ip_seconds", "shared_networks"):
        assert not hasattr(throttle_mod, name)
