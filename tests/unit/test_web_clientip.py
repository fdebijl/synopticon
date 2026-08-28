"""web.clientip: the one place X-Forwarded-* is believed.

Named tests here pin the corrections the security contract calls out
explicitly (D3, D5, D7, R9, R11) so a regression is loud.
"""

from __future__ import annotations

import asyncio
import ipaddress

import pytest

from synopticon.web import clientip


def _scope(*, client=None, headers=(), scheme="http", type_="http"):
    return {
        "type": type_,
        "client": client,
        "scheme": scheme,
        "headers": [(k.encode("latin-1"), v.encode("latin-1")) for k, v in headers],
    }


async def _run_proxy_headers(app_scope, trusted):
    captured = {}

    async def inner_app(scope, receive, send):
        captured["scope"] = scope

    async def receive():  # pragma: no cover - never awaited by ProxyHeaders
        return {}

    async def send(message):  # pragma: no cover - never called by ProxyHeaders
        pass

    mw = clientip.ProxyHeaders(inner_app, trusted)
    await mw(app_scope, receive, send)
    return captured["scope"]


def run_proxy_headers(app_scope, trusted):
    return asyncio.run(_run_proxy_headers(app_scope, trusted))


# -- ip_prefix ---------------------------------------------------------------


def test_ip_prefix_normalizes_ipv4_mapped_ipv6():
    assert clientip.ip_prefix("::ffff:192.168.1.5") == clientip.ip_prefix("192.168.1.5")


def test_ip_prefix_unparseable_passthrough_is_bounded():
    garbage = "x" * 500
    assert clientip.ip_prefix(garbage) == garbage[: clientip.IP_MAX]


# -- resolve_client / ProxyHeaders agreement (D3) -----------------------------


def test_proxy_headers_agrees_with_resolve_client():
    trusted = [ipaddress.ip_network("10.0.0.1/32")]
    scope = _scope(
        client=("10.0.0.1", 1234),
        headers=[("x-forwarded-for", "203.0.113.9")],
    )
    forwarded_for = clientip.forwarded_header(scope)
    expected = clientip.resolve_client("10.0.0.1", forwarded_for, trusted, scheme="http")

    out_scope = run_proxy_headers(dict(scope), trusted)
    assert out_scope[clientip.SCOPE_KEY] == expected


def test_proxy_headers_never_overwrites_scope_client_or_scheme():
    """D3: scope['client'] and scope['scheme'] stay byte-identical."""
    trusted = [ipaddress.ip_network("10.0.0.1/32")]
    scope = _scope(
        client=("10.0.0.1", 1234),
        headers=[("x-forwarded-for", "203.0.113.9")],
        scheme="http",
    )
    out_scope = run_proxy_headers(dict(scope), trusted)
    assert out_scope["client"] == ("10.0.0.1", 1234)
    assert out_scope["scheme"] == "http"
    assert clientip.SCOPE_KEY in out_scope


# -- untrusted peer: header ignored but echoed --------------------------------


def test_untrusted_peer_xff_never_changes_resolved_ip_but_is_echoed():
    resolved = clientip.resolve_client("203.0.113.5", "9.9.9.9", trusted=[])
    assert resolved.ip == "203.0.113.5"
    assert resolved.source == "socket_peer"
    assert resolved.forwarded_for == "9.9.9.9"


# -- RFC 9110: every header line is joined, never truncated to the first (R9) -


def test_forwarded_header_joins_two_header_lines():
    scope = _scope(headers=[("x-forwarded-for", "1.1.1.1"), ("x-forwarded-for", "2.2.2.2")])
    assert clientip.forwarded_header(scope) == "1.1.1.1, 2.2.2.2"


def test_two_header_lines_walk_takes_rightmost_of_the_joined_value():
    trusted = [ipaddress.ip_network("10.0.0.1/32")]
    scope = _scope(
        client=("10.0.0.1", 1),
        headers=[("x-forwarded-for", "203.0.113.9"), ("x-forwarded-for", "8.8.8.8")],
    )
    forwarded_for = clientip.forwarded_header(scope)
    assert forwarded_for == "203.0.113.9, 8.8.8.8"
    resolved = clientip.resolve_client("10.0.0.1", forwarded_for, trusted)
    assert resolved.ip == "8.8.8.8"
    assert resolved.source == "forwarded"


# -- resolve_client never returns raw header text (R11) -----------------------


def test_resolve_client_never_returns_unparsed_garbage():
    resolved = clientip.resolve_client("not-an-ip", "also-not-an-ip", trusted=[])
    assert resolved.ip == clientip.UNKNOWN_IP
    assert resolved.peer == clientip.UNKNOWN_IP

    # A trusted peer sending a header that does not parse: the walk stops
    # immediately (an unparseable entry is never returned) and `ip` falls back
    # to the canonical peer -- never the raw garbage text.
    trusted = [ipaddress.ip_network("10.0.0.1/32")]
    resolved = clientip.resolve_client("10.0.0.1", "definitely garbage", trusted)
    assert resolved.ip == "10.0.0.1"
    assert resolved.source == "socket_peer"


# -- D7: a trusted loopback peer forwarding a forged header is honestly believed --


def test_trusted_loopback_peer_forged_header_is_believed_and_documented():
    trusted = [ipaddress.ip_network("127.0.0.1/32")]
    resolved = clientip.resolve_client("127.0.0.1", "203.0.113.77", trusted)
    assert resolved.ip == "203.0.113.77"
    assert resolved.source == "forwarded"
    assert resolved.peer_trusted is True


# -- D5: no exemption predicate exists at the module surface -----------------


@pytest.mark.parametrize("name", ["local_request", "stands_in_for", "address_tiers_armed"])
def test_no_exemption_predicate_exists(name):
    assert not hasattr(clientip, name)


# -- resolved() fallback for a request never touched by the middleware -------


def test_resolved_falls_back_to_socket_peer_when_middleware_absent():
    scope = _scope(client=("192.168.1.9", 1))
    resolved = clientip.resolved(scope)
    assert resolved.ip == "192.168.1.9"
    assert resolved.source == "socket_peer"
    assert resolved.peer_trusted is False
    assert resolved.forwarded_for == ""


def test_client_ip_is_unknown_for_testclient_style_peer():
    scope = _scope(client=("testclient", 1))
    assert clientip.client_ip(scope) == clientip.UNKNOWN_IP


# -- IPAllowlist ---------------------------------------------------------------


def test_allowlist_off_allows_everything():
    allowlist = clientip.IPAllowlist([])
    assert allowlist.active is False
    assert allowlist.allows("203.0.113.5") is True
    assert allowlist.allows(None) is True


def test_allowlist_allows_loopback_unconditionally():
    allowlist = clientip.IPAllowlist(["203.0.113.0/24"], allow_private=False)
    assert allowlist.allows("127.0.0.1") is True


def test_allowlist_refuses_unknown_ip_when_active():
    allowlist = clientip.IPAllowlist(["203.0.113.0/24"])
    assert allowlist.allows(clientip.UNKNOWN_IP) is False
    assert allowlist.allows(None) is False


def test_allowlist_allows_private_when_enabled():
    allowlist = clientip.IPAllowlist(["203.0.113.0/24"], allow_private=True)
    assert allowlist.allows("192.168.1.5") is True


def test_allowlist_matches_configured_entry():
    allowlist = clientip.IPAllowlist(["203.0.113.0/24"], allow_private=False)
    assert allowlist.allows("203.0.113.5") is True
    assert allowlist.allows("8.8.8.8") is False


def test_allowlist_adds_nothing_when_every_entry_is_already_private():
    allowlist = clientip.IPAllowlist(["192.168.1.0/24"], allow_private=True)
    assert allowlist.adds_nothing() is True

    allowlist = clientip.IPAllowlist(["203.0.113.0/24"], allow_private=True)
    assert allowlist.adds_nothing() is False


def test_allowlist_describe_shape():
    allowlist = clientip.IPAllowlist(["203.0.113.0/24"], allow_private=True)
    described = allowlist.describe()
    assert described["active"] is True
    assert described["loopback_always"] is True
    assert described["entries"] == ["203.0.113.0/24"]
