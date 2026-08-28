"""Who a request came from: the one place X-Forwarded-* is believed.

Four features key on the client address -- the network allowlist, the sign-in
log, session pinning and login throttling -- and all four are only as sound as
the address they are handed. So the trust decision is made exactly once, here,
and everything else calls `client_ip`.

This module resolves an address and does NOTHING ELSE. It exposes no predicate
that arms or disarms a defence, because no defence in this codebase is
conditional (RULE 2). Do not add one back: three revisions tried, and every
version of "this request is really the server" was satisfied by every visitor on
the deployment our own README recommends.

uvicorn's own ProxyHeadersMiddleware is switched off explicitly in `serve()`
(proxy_headers=False; it defaults to ON). It used to run with
forwarded_allow_ips="*", which believes the header from any peer -- that is what
made the per-address login throttle free to defeat with a random X-Forwarded-For
per attempt. Doing it in-process instead also means it is reachable from
TestClient, which never starts uvicorn, so the allowlist, the pin and the
throttle are all testable end to end.

Unlike uvicorn's, this middleware does NOT rewrite scope['client'] or
scope['scheme']. It attaches a ResolvedClient under scope['synopticon.client']
and leaves the socket peer where every other consumer expects to find it -- the
PUT /api/config lockout guard has to judge a *proposed* trust list against the
*peer*, and GET /api/security/access has to be able to say "you reached me from
172.18.0.4, which is your proxy".

Stdlib only, by rule: web/auth/* and web/app.py may import this, and it may
import none of them.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import threading
from typing import Any, Iterable, NamedTuple, Sequence

log = logging.getLogger("synopticon.web")

Network = ipaddress.IPv4Network | ipaddress.IPv6Network
Address = ipaddress.IPv4Address | ipaddress.IPv6Address

#: The ASGI scope key ProxyHeaders attaches its ResolvedClient under. Namespaced
#: so it can never collide with a server's or another middleware's key.
SCOPE_KEY = "synopticon.client"

#: Allowed by IPAllowlist unconditionally (RULE 4). The anti-lockout guarantee:
#: curl on the box itself, the container healthcheck and an
#: `ssh -L 8686:127.0.0.1:8686 nas` tunnel can never be shut OUT by a bad list.
#: It is NOT an exemption from anything else -- the throttle throttles loopback
#: like any other address (RULE 2, D5).
LOOPBACK: tuple[Network, ...] = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
)

#: RFC1918 + CGNAT + link-local + unique-local: the "my LAN" shorthand. Consumed
#: by IPAllowlist's `allow_private` only.
PRIVATE: tuple[Network, ...] = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)

#: Returned when there is no peer at all (a unix socket, or a test transport
#: that supplies none), and when a peer string does not parse as an address.
#: Never a valid address, so it can never match a configured network -- an
#: allowlist that is on refuses it, by design. It IS a valid throttle key, and
#: it is throttled like any other, because D5 leaves no exempt category for a
#: degraded record to fall into.
UNKNOWN_IP = "?"

#: Longest User-Agent stored or logged, anywhere. authlog._LOG_UA_MAX is defined
#: AS this constant and app.py uses this one directly; there is exactly one
#: value and no module reaches into another's private name for it.
UA_MAX = 200

#: Longest username any component keeps. A username is unbounded request input
#: and it is a dictionary key inside the anti-DoS limiter and a column in
#: web_login_challenges: `max_tracked` bounds the number of entries, not their
#: size, so without this 4096 attempts carrying 1 MB usernames is 4 GB inside
#: the mitigation. Enforced in LoginRateLimiter's key builder, in
#: twofactor.start_login_challenge's INSERT, and in authlog.record_attempt
#: (whose _LOG_USERNAME_MAX is defined AS this constant).
USERNAME_MAX = 128

#: Longest address string kept anywhere. A parsed address is far shorter; this
#: only bounds ip_prefix's unparseable-passthrough branch.
IP_MAX = 64


class ClientFacts(NamedTuple):
    """What a request reveals about its client: (user_agent, ip).

    The two properties a session can be pinned to. Constructed by
    `client_facts`; nothing in web/auth ever sees a Request.
    """

    user_agent: str = ""
    ip: str = ""


class ResolvedClient(NamedTuple):
    """RULE 1's answer for one request, attached at scope[SCOPE_KEY].

    ip              THE client address. The only value any defence may key on.
                    Canonical str(ipaddress.ip_address(...)) or UNKNOWN_IP,
                    always -- never raw header text.
    source          "forwarded" | "socket_peer" -- which of RULE 1's two
                    branches produced `ip`. DIAGNOSTIC ONLY: it is reported by
                    GET /api/security/access and read by nothing that decides
                    anything. No throttle, gate or pin may branch on it (D5).
    peer            The socket peer, always, unchanged. Same normalisation as
                    `ip`. UNKNOWN_IP when absent.
    forwarded_for   The RFC 9110 comma-join of every X-Forwarded-For line,
                    verbatim, or "". Echoed for diagnostics and read by the
                    PUT /api/config guards; no defence branches on it.
    scheme          The effective scheme: X-Forwarded-Proto when the peer is a
                    trusted proxy and the header is "http" or "https",
                    otherwise scope["scheme"].
    peer_trusted    True when `peer` falls inside a configured trusted_proxies
                    network. Server-derived. Diagnostic and config-guard input;
                    no throttle tier reads it.
    """

    ip: str
    source: str
    peer: str
    forwarded_for: str
    scheme: str
    peer_trusted: bool


class NetworkListError(ValueError):
    """A configured entry is not an IP address or CIDR range. Names the entry."""


def parse_networks(entries: Sequence[str]) -> list[Network]:
    """Config entries -> networks. A bare address becomes a single-host network.

    strict=False, so '192.168.1.5/24' is accepted and masked to 192.168.1.0/24.
    Raises NetworkListError naming the offending entry.
    """
    networks: list[Network] = []
    for entry in entries:
        try:
            networks.append(ipaddress.ip_network(entry.strip(), strict=False))
        except ValueError as exc:
            raise NetworkListError(f"{entry!r} is not an IP address or CIDR range") from exc
    return networks


_ZONE_RE = re.compile(r"%.*$")


def normalize_ip(raw: str | None) -> Address | None:
    """Peer address string -> comparable address, or None if it is not one.

    Strips '[...]' brackets and a '%zone' scope id, and unwraps an IPv4-mapped
    IPv6 address ('::ffff:192.168.1.5' -> '192.168.1.5') so a dual-stack
    listener's peers still match an IPv4 CIDR. Returns None for '', '?',
    'testclient' and anything else unparseable.
    """
    if not raw:
        return None
    candidate = raw.strip()
    if candidate.startswith("[") and "]" in candidate:
        candidate = candidate[1 : candidate.index("]")]
    candidate = _ZONE_RE.sub("", candidate)
    if not candidate:
        return None
    try:
        addr = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address):
        mapped = addr.ipv4_mapped
        if mapped is not None:
            return mapped
    return addr


def in_networks(ip: str | None, networks: Iterable[Network]) -> bool:
    """True when `ip` parses and falls inside any of `networks`."""
    addr = normalize_ip(ip)
    if addr is None:
        return False
    return any(addr in net for net in networks)


def is_loopback(ip: str | None) -> bool:
    """True when `ip` parses and falls inside LOOPBACK.

    Read by IPAllowlist.allows (RULE 4), by the PUT /api/config guards, and by
    the start-up diagnostics. NOT read by throttle.py -- the throttle has no
    address it treats specially (D5).
    """
    return in_networks(ip, LOOPBACK)


def ip_prefix(ip: str) -> str:
    """The network an address belongs to: /24 for IPv4, /64 for IPv6.

    Normalised through normalize_ip first, so a dual-stack listener's IPv4-mapped
    form ('::ffff:192.168.1.5') yields the same prefix as the plain IPv4 one --
    otherwise the same browser flips prefix per connection, which destroys its
    own pinned session and splits its own throttle bucket.

    /24 and /64 are the smallest allocations a consumer ISP hands out, which is
    what makes this the right key for both pinning and throttling: keying on a
    full 128-bit IPv6 address would give an attacker 2**64 free buckets and churn
    the limiter's bounded table at the same time.

    Genuinely unparseable input ('?', 'testclient', 'ip1') is returned verbatim
    but TRUNCATED TO IP_MAX, so it still keys consistently rather than
    collapsing to "" -- and cannot be an unbounded dictionary key.
    """
    addr = normalize_ip(ip)
    if addr is None:
        return (ip or "")[:IP_MAX]
    prefix_len = 24 if isinstance(addr, ipaddress.IPv4Address) else 64
    net = ipaddress.ip_network(f"{addr}/{prefix_len}", strict=False)
    return str(net)


def forwarded_header(request_or_scope: Any) -> str:
    """Every X-Forwarded-For line on this request, comma-joined in order.

    RFC 9110 section 5.3: N occurrences of a list-valued field are equivalent to
    one occurrence with the values comma-joined, in order. Starlette's
    `headers.get` returns only the FIRST line, and a raw scope['headers'] walk
    returns all of them -- so the middleware and the PUT /api/config guard would
    disagree about which value is "the" header on a chain that emits two lines.
    Both call this. Accepts a Starlette Request (uses headers.getlist) or a raw
    ASGI scope (walks scope['headers']).
    """
    headers = getattr(request_or_scope, "headers", None)
    if headers is not None and hasattr(headers, "getlist"):
        return ", ".join(headers.getlist("x-forwarded-for"))
    scope = request_or_scope
    values = [
        value.decode("latin-1")
        for key, value in scope.get("headers", ())
        if key == b"x-forwarded-for"
    ]
    return ", ".join(values)


_UA_VERSION_RE = re.compile(r"[/ ]\d[\w.]*")


def device_key(user_agent: str) -> str:
    """A User-Agent reduced to what actually identifies a device.

    Every version number is stripped, whitespace is collapsed and the result is
    lowercased and truncated to UA_MAX. Chrome and Edge bump their UA major
    version roughly monthly and iOS/Android bump theirs on OS update, so pinning
    the raw header schedules a forced sign-out -- plus, on a 2FA instance, a full
    password-and-code dance -- for every pinned session on a four-week cycle.

    The trade is deliberate and small: a thief on the same OS with a different
    browser *version* now matches where they previously would not. A pin is a
    replay speed bump, not an authentication factor; a monthly self-inflicted
    sign-out is the larger harm. Unrecognised input is returned lowercased and
    whitespace-collapsed rather than dropped, so it still pins.
    """
    reduced = _UA_VERSION_RE.sub("", user_agent or "")
    reduced = " ".join(reduced.split())
    return reduced.lower()[:UA_MAX]


def resolve_client(
    peer: str | None,
    forwarded_for: str | None,
    trusted: Sequence[Network],
    *,
    forwarded_proto: str | None = None,
    scheme: str = "http",
) -> ResolvedClient:
    """RULE 1, as a pure function. No Request, no scope, no I/O.

    `forwarded_for` is the comma-joined value `forwarded_header` produces. It is
    a single string here so that this function stays pure and testable; the
    joining is the caller's job and there is exactly one joiner.

    Factored out so a *hypothetical* trust list can be evaluated without
    restarting: PUT /api/config must judge a proposed allowlist against the
    proposed trusted_proxies in the same save, and the running middleware was
    built from the old list. The realistic first-time configuration -- proxy,
    allowlist and allow_private_networks=false, three fields on one Settings tab
    behind one save bar -- would otherwise be refused by the very guard that
    exists to protect it, training the admin to pass ?allow_lockout=1 forever.

    When `peer` is inside `trusted`: split X-Forwarded-For on commas and walk
    from the RIGHT, skipping entries that parse and are themselves inside
    `trusted`, and take the first entry that parses and is not. An entry that
    does not parse stops the walk, and whatever was found so far is kept -- an
    unparseable entry is never returned. If the walk yields nothing, `ip` is the
    peer and `source` is "socket_peer": a trusted proxy that sends no usable
    header is standing in for its visitors, and every one of them shares that
    address's throttle bucket and allowlist verdict.

    IMPORTANT, and the honest half of D7: when `peer` is inside `trusted`, this
    walk runs on data the proxy chose to send. A proxy that does not OVERWRITE
    X-Forwarded-For -- nginx relays client request headers to the upstream by
    default, so a bare `proxy_pass` with no `proxy_set_header` line is exactly
    this case -- passes the visitor's own header through, and the rightmost
    untrusted entry is then whatever the visitor typed. Listing a proxy is
    therefore a statement that the proxy is trustworthy AND that it overwrites
    the header. There is no way to verify the second from inside the process (a
    single-entry header is what a correct overwrite produces and what a naive
    forgery looks like), so it is documented at every surface instead of
    detected: see the trusted_proxies field text, both start-up warnings,
    GET /api/security/access, NetworkAccessCard, and the README block.

    When `peer` is NOT inside `trusted`: `ip` is the peer, `source` is
    "socket_peer", and X-Forwarded-For is ignored entirely. It is still echoed
    in `forwarded_for` so route 17 can show the operator what was ignored. THIS
    IS THE DEFAULT (`trusted` empty) AND IT IS SAFE: no header is ever believed,
    and no client can influence its own resolved address.

    Every returned address goes through normalize_ip; a peer that does not parse
    yields UNKNOWN_IP for both `ip` and `peer`.
    """
    forwarded_for = forwarded_for or ""
    peer_addr = normalize_ip(peer)
    peer_str = str(peer_addr) if peer_addr is not None else UNKNOWN_IP
    peer_trusted = peer_addr is not None and any(peer_addr in net for net in trusted)

    ip: str | None = None
    if peer_trusted:
        parts = [p.strip() for p in forwarded_for.split(",") if p.strip()]
        for part in reversed(parts):
            addr = normalize_ip(part)
            if addr is None:
                break
            if any(addr in net for net in trusted):
                continue
            ip = str(addr)
            break

    if ip is not None:
        source = "forwarded"
    else:
        ip = peer_str
        source = "socket_peer"

    effective_scheme = scheme
    if peer_trusted and forwarded_proto in ("http", "https"):
        effective_scheme = forwarded_proto

    return ResolvedClient(
        ip=ip,
        source=source,
        peer=peer_str,
        forwarded_for=forwarded_for,
        scheme=effective_scheme,
        peer_trusted=peer_trusted,
    )


# -- accessors: the only ways to ask about a request's client ---------------- #


def _scope(request_or_scope: Any) -> Any:
    """The raw ASGI scope dict, whether given a Starlette Request or a scope."""
    return getattr(request_or_scope, "scope", request_or_scope)


def resolved(request_or_scope: Any) -> ResolvedClient:
    """The ResolvedClient at scope[SCOPE_KEY].

    Falls back to a socket-peer-only ResolvedClient built from scope['client']
    when the key is absent -- a route exercised without the middleware (a unit
    test, a sub-application) must degrade to "the peer", never crash and never
    silently believe a header. The fallback sets forwarded_for="",
    peer_trusted=False and source="socket_peer".

    Under D5 the degraded record has no security consequence at all: there is no
    predicate that reads `source`, `forwarded_for` or `peer_trusted` to decide
    whether a tier applies, so "we lost the trust facts" and "we have them"
    throttle identically. (An earlier draft called this the "most-armed reading"
    while a loopback-valued degraded record was in fact fully exempt.)
    """
    scope = _scope(request_or_scope)
    existing = scope.get(SCOPE_KEY)
    if existing is not None:
        return existing
    client = scope.get("client")
    peer_raw = client[0] if client else None
    peer_addr = normalize_ip(peer_raw)
    peer_str = str(peer_addr) if peer_addr is not None else UNKNOWN_IP
    return ResolvedClient(
        ip=peer_str,
        source="socket_peer",
        peer=peer_str,
        forwarded_for="",
        scheme=scope.get("scheme", "http"),
        peer_trusted=False,
    )


def client_ip(request_or_scope: Any) -> str:
    """THE client address, as a string. The only accessor a defence may use.

    Accepts a Starlette Request or a raw ASGI scope; returns `resolved(x).ip`.

    The value is a *string*: it may be UNKNOWN_IP (including under TestClient's
    default transport, whose peer 'testclient' does not parse). Every consumer
    must cope -- the allowlist refuses it when active, the throttle keys on its
    prefix, the pin passes it through unchanged, and the log stores it as-is.
    A test that exercises the allowlist must therefore construct
    `TestClient(app, client=("192.168.1.5", 1234))` rather than rely on the
    default peer.
    """
    return resolved(request_or_scope).ip


def client_peer(request_or_scope: Any) -> str:
    """The socket peer, never a forwarded address. For the PUT /api/config
    lockout guards and route 17's diagnostic, both of which have to reason about
    the connection itself rather than about who it claims to carry."""
    return resolved(request_or_scope).peer


def client_scheme(request_or_scope: Any) -> str:
    """The effective scheme ('http' | 'https').

    `_set_session_cookie` reads this instead of `request.url.scheme`, which is
    what gives the session cookie its Secure flag behind a TLS-terminating proxy
    now that the scope is left alone.
    """
    return resolved(request_or_scope).scheme


def user_agent(request_or_scope: Any) -> str:
    """The User-Agent header, or '' when absent. Never truncated here."""
    scope = _scope(request_or_scope)
    for key, value in scope.get("headers", ()):
        if key == b"user-agent":
            return value.decode("latin-1")
    return ""


def client_facts(request_or_scope: Any) -> ClientFacts:
    """The two properties a session can be pinned to: (user_agent, ip)."""
    return ClientFacts(user_agent=user_agent(request_or_scope), ip=client_ip(request_or_scope))


def _covered_by_private(net: Network) -> bool:
    for private in PRIVATE:
        if net.version == private.version and net.subnet_of(private):
            return True
    return False


class IPAllowlist:
    """Which client addresses may reach the GUI. Pure CPU, no I/O, thread-safe.

    An empty `entries` list means the feature is off and every address is
    allowed. That is the default, so an upgrade changes nothing.
    """

    def __init__(self, entries: Sequence[str], allow_private: bool = True) -> None:
        self._networks = parse_networks(entries)
        self._allow_private = allow_private
        self._active = bool(self._networks)
        self._lock = threading.Lock()
        self._memo: dict[str, bool] = {}

    @property
    def active(self) -> bool:
        """True when entries were configured, i.e. the gate actually refuses."""
        return self._active

    def allows(self, ip: str | None) -> bool:
        """True if `ip` may be served. Always True when not `active`.

        Loopback is allowed unconditionally -- RULE 4. There is no second
        parameter and there must not be one: an earlier draft took a
        `local_request` flag it recorded and never used, and the only thing it
        could ever have been used for -- narrowing the loopback allowance -- is
        the one thing RULE 4 forbids, because `synopticon web-access --clear`
        over an SSH tunnel is the recovery path section 5.4 advertises.

        Private ranges are allowed when `allow_private`. An unparseable or
        missing address (UNKNOWN_IP) is REFUSED when active: there is no peer to
        judge, and failing closed is the only safe direction for a gate.

        Memoized on the string form in a dict bounded at 4096 entries, cleared
        wholesale when exceeded -- a review page is one request per crop, the
        entries are per-address and unauthenticated, and losing one costs a
        parse. (That is why `clear()` is acceptable here and NOT acceptable for
        the auth cache -- see F4 and section 6 step 20.)
        """
        if not self._active:
            return True
        if not ip:
            return False
        with self._lock:
            cached = self._memo.get(ip)
        if cached is not None:
            return cached
        result = self._compute(ip)
        with self._lock:
            if len(self._memo) >= 4096:
                self._memo.clear()
            self._memo[ip] = result
        return result

    def _compute(self, ip: str) -> bool:
        if is_loopback(ip):
            return True
        addr = normalize_ip(ip)
        if addr is None:
            return False
        if self._allow_private and in_networks(ip, PRIVATE):
            return True
        return in_networks(ip, self._networks)

    def adds_nothing(self) -> bool:
        """True when every configured entry already falls inside PRIVATE while
        `allow_private` is on -- i.e. the list is active but restricts nobody
        who was not already refused. The Access tab says so in words; without it
        the combination is unreadable from the two inputs, which sit three rows
        apart in the Settings form.

        Note this does NOT cover the other way a list restricts nobody -- every
        visitor resolving to one address behind a proxy. That is not a property
        of the list, so it is reported by route 17 and refused by section 5.3's
        guards instead."""
        if not self._active or not self._allow_private:
            return False
        return all(_covered_by_private(net) for net in self._networks)

    def describe(self) -> dict[str, Any]:
        """{'active', 'entries', 'allow_private', 'loopback_always', 'adds_nothing'}."""
        return {
            "active": self._active,
            "entries": [str(net) for net in self._networks],
            "allow_private": self._allow_private,
            "loopback_always": True,
            "adds_nothing": self.adds_nothing(),
        }


class ProxyHeaders:
    """Attach a ResolvedClient to every request. Pure ASGI, no I/O.

    Registered OUTERMOST, so every layer beneath (including _AuthMiddleware's
    allowlist gate and its /api/health short-circuit) sees the resolution.

    It does NOT write scope['client'] or scope['scheme'] (RULE 3). It sets
    scope[clientip.SCOPE_KEY] = resolve_client(...) and nothing else. It builds
    the header value with `forwarded_header(scope)` and delegates the decision
    to `resolve_client`, so the middleware and the PUT /api/config guard can
    never drift apart on either half.

    Pure dict lookups plus one header join and split -- no I/O -- so it is safe
    in front of the health probe.
    """

    def __init__(self, app, trusted: Sequence[Network]) -> None:
        self._app = app
        self._trusted = list(trusted)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        client = scope.get("client")
        peer = client[0] if client else None
        forwarded_for = forwarded_header(scope)
        forwarded_proto = None
        for key, value in scope.get("headers", ()):
            if key == b"x-forwarded-proto":
                forwarded_proto = value.decode("latin-1").split(",")[0].strip()
                break
        scope[SCOPE_KEY] = resolve_client(
            peer,
            forwarded_for,
            self._trusted,
            forwarded_proto=forwarded_proto,
            scheme=scope.get("scheme", "http"),
        )
        await self._app(scope, receive, send)
