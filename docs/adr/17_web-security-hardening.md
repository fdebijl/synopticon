# ADR 17 — Web security hardening

**Status:** Accepted
**Applies to:** `web/clientip.py`, `web/auth/*`, `web/totp.py`, `web/security_routes.py`, `web/app.py`'s middleware stack and login routes, `web/configio.py`'s `guarded_write`, `web/scheduler.py`'s housekeeping pass, `db/migrations/0010_web_security.sql`, `db/snapshot.py`, `db/copy.py`, `cli.py`'s `web-access`/`disable-2fa`/`session-pin`

## Context

Five features landed on the web auth surface in one pass — two-step sign-in (TOTP), a network
allowlist, a sign-in log, session pinning, and login throttling — because they all key on the same
fact: who is on the other end of this connection. Designing them one at a time produced three
separate, independently-plausible ways to get that fact wrong, and this ADR exists to make sure a
fourth attempt is never made.

The target is a single-admin, self-hosted tool, not a multi-tenant SaaS. That shapes every decision
below: there is no "wrong account, right instance" case to defend against, the operator *is* the
person who can fix a lockout, and the worst outcome for most of these features is not "an attacker
gets in" but "the admin locks themselves out and has no way back but a reinstall."

## Decision

### D1 — One trust boundary, and it exposes no predicate

`web/clientip.py` resolves exactly one address for a request and does nothing else. Every feature
that keys on an address — the allowlist, the sign-in log, session pinning, login throttling — calls
`client_ip`/`client_facts`/`resolved` and never touches a header itself. `ProxyHeaders`, the
outermost ASGI middleware, attaches the resolution at `scope["synopticon.client"]` and leaves
`scope["client"]` and `scope["scheme"]` untouched (**D1a**, below explains why not overwriting
matters). `resolve_client` is a pure function of `(peer, forwarded_for, trusted)` so the exact same
decision can be re-run hypothetically against a *proposed* trust list, which `configio.guarded_write`
needs (D7) and a stateful middleware could not give it.

`forwarded_header` comma-joins every `X-Forwarded-For` *line* before the right-to-left walk, per
RFC 9110 §5.3 ("N occurrences of a list-valued field are equivalent to one occurrence with the
values comma-joined, in order"). Starlette's `headers.get` returns only the first line; a raw scope
walk returns all of them. Both the middleware and the `PUT /api/config` guard call the same
`forwarded_header` for this reason — two independent joiners would let the two disagree about which
value is "the" header on a chain that emits it twice.

#### D1a — Why `ProxyHeaders` adds a key instead of overwriting `scope["client"]`

Two consumers need both halves of the truth at once. `GET /api/security/access` has to be able to
say "you reached me from 172.18.0.4 [the proxy], which resolves you to 203.0.113.9 [the visitor]" —
that sentence is unwritable if the peer was already overwritten. And `configio.guarded_write` has to
judge a *proposed* `trusted_proxies` list against the *actual socket peer*, because the realistic
first-time save adds a proxy to the trust list and an allowlist entry in the same `PUT` — judging
against what the running (old) middleware resolved would refuse exactly the save that fixes the
configuration. So `ProxyHeaders` is additive: `scope["synopticon.client"]` carries the resolution,
`scope["client"]` stays the raw peer forever, and `clientip.client_peer` is the accessor for callers
that need the connection itself rather than who it claims to carry.

#### D1b — The honest statement of the reverse-proxy hazard

The default `trusted_proxies = []` is safe because no header is ever believed and no client can
influence its own resolved address. Listing a proxy is a statement of two things at once: that the
peer is trustworthy, *and* that it overwrites `X-Forwarded-For` rather than relaying the visitor's
own value. nginx does the second wrong by default — a bare `proxy_pass` with no `proxy_set_header`
line relays client request headers straight through — so listing an nginx proxy without also adding
`proxy_set_header X-Forwarded-For $remote_addr;` lets any visitor claim to be any address, which
takes the allowlist, both throttle tiers, `EventThrottle`'s row bound and the network half of a
session pin with it in one move.

**No detector is written for this, on purpose.** A single-entry `X-Forwarded-For` header is what a
correct overwrite produces, and it is also exactly what a naive forgery looks like — there is no
byte pattern to distinguish "the proxy overwrote this" from "the visitor forged this and the proxy
passed it through unchanged." Any check either false-positives on a correctly configured proxy or
false-negatives on the forgery it exists to catch. The resolution is documentation at every surface
that touches the decision instead: the `trusted_proxies` field's own help text, two start-up
warnings (an `allow_from` set with no `trusted_proxies`, and a `trusted_proxies` entry that overlaps
loopback), `GET /api/security/access`'s `proxy` block, `NetworkAccessCard`, and the README's
"Behind a reverse proxy" section. Listing a loopback address without the `proxy_set_header` line is
called out explicitly as *worse* than listing nothing, because it arms the belief without fixing
the proxy that would make it safe.

### D2 — No defense may branch on a request header, and then no defense may branch on anything

The first drafts of the address-tier throttle stood the tiers down whenever a request carried an
*untrusted* `X-Forwarded-For` — reasoning that an untrusted header is "probably the operator testing
from curl." On the default configuration (`trusted_proxies` empty), *every* header is untrusted, so
one line of curl (`curl -H 'X-Forwarded-For: 1.2.3.4' ...`, repeated with a new value each time)
disarmed two of the three tiers for anyone. The second draft narrowed that to a loopback exemption.
The third replaced it with a four-term `local_request` predicate (loopback peer, or a trusted proxy
standing in for a peer with no forwarded header, or ...). On the deployment this project's own
README recommends — Synopticon bound to loopback, nginx on the same host, default config — *every
visitor on the internet* satisfies that predicate, because they all arrive at the socket as
127.0.0.1 via the proxy. Two sections of the contract that produced this project asserted the
address tiers were armed on that exact deployment while this predicate had switched them off for
the whole internet.

The fix is not a fifth, narrower exemption. **Every tier is armed on every request, keyed on the one
resolved address, and `clientip` exposes no predicate a defense could read to stand itself down.**
`LoginRateLimiter.verdict` takes a `client` argument and reads exactly one field of it — `.ip` — and
its docstring says, in block capitals, that there is no trust parameter and never may be one.
`EventThrottle` and the network allowlist are built the same way. Record all three drafts here,
because the failure mode this ADR guards against is a plausible-looking exemption, not a careless
one — every one of the three passed review on its own terms.

#### D2a — Why the global throttle tier was deleted rather than tuned

An early design added a third, instance-wide tier: block *every* sign-in attempt, regardless of
address or username, once total failures crossed a threshold. On a multi-tenant service that is a
reasonable brake. On a single-admin self-hosted tool it is an admin-lockout primitive that an
attacker can trigger with bandwidth from addresses the admin does not control — and its proposed
escapes made it worse, not better: a "known-good" address set that a container restart empties, and
a LAN exemption that fails in exactly the moment it fires (a remote admin, travelling, is not on the
LAN). Two concrete findings died with the tier: a cross-principal refund (one user's successful
sign-in could clear the global counter an *attacker* had built up, which is backwards — it rewards
the attacker for making the legitimate user's life harder) and a 429 response whose recovery text
named a config key that did not govern the global tier at all and, if followed, would have made the
lockout worse. Deleting the tier deleted both findings at once. What remains — the per-(scope,
address-prefix, username) pair tier and the per-address-prefix tier — bounds the same abuse
(credential stuffing, password spraying) without a knob that turns off sign-in for the admin too.

#### D2b — Why loopback is allowed unconditionally by the allowlist and throttled unconditionally by the limiter

Two features, one address, opposite treatment, and it is deliberate. `IPAllowlist.allows` returns
`True` for loopback no matter what is configured (`clientip.LOOPBACK`, RULE 4) — its failure mode is
a *permanent* lockout, and the recovery path, `synopticon web-access --clear` run over an SSH tunnel
or a console on the box, necessarily arrives on loopback. Exempting loopback from the allowlist is
what keeps that recovery path alive through any list an operator could write. `LoginRateLimiter`
exempts nothing, loopback included — its failure mode is a five-minute wait, not a lockout, and a
throttle that stood down for "the server itself" is D2's whole story: on the documented deployment
every visitor *is* the server itself, once a proxy is in the picture.

### D3 — Login throttling: the challenge oracle, and why `record_pending` is a separate charge from `record_failure`

Once any account on the instance has two-step sign-in enabled, a wrong password and an unknown
username must produce the *same* first-step response as a right one — a `{"mfa_required": true,
...}` challenge token — or the response itself becomes a username oracle (a real, TOTP-enrolled
username produces a challenge; anything else produces a flat 401). `auth.challenge_required`
implements this: it returns `True` whenever `any_totp_enrolled(conn)` is true, independent of
whether the password check upstream actually succeeded, and `web_login_challenges.user_id` is
`NULL` for the wrong-password case — same table, same TTL, same shape, indistinguishable from
outside.

That symmetry creates a second problem: an attacker who knows this now sends a flood of *wrong*
passwords, each of which returns a challenge token that is never going to be redeemed, and none of
those are a "failure" `LoginRateLimiter.record_failure` would recognize — there is no wrong password
to react to, because the whole point was not revealing whether it was wrong. `record_pending` is the
answer: it charges the per-pair *attempt* window (a sliding count of `pair_max_attempts` attempts,
successful-looking or not, in `pair_window_seconds`) and the per-address window, but it never arms
the pair's exponential backoff — that backoff is reserved for a *definitive* failure recorded later,
at the second step, once the code is checked. Two independent counters on the same row
(`_PairEntry.attempts` for the sliding window, `_PairEntry.failures` for the backoff) is what lets
"an address is spraying passwords, we just can't prove which ones were wrong yet" and "this specific
account just failed for the Nth time" be bounded by different mechanisms with different intents.

`record_success`'s refund is pair-exact, never a bulk exemption. A completed sign-in removes only
the address-window entries that its *own* `(scope, ip_prefix, username)` pair charged — the window
entry carries the charging pair's key, and the refund filters by equality on that key, never "pop
the newest N" and never "clear this whole /24 for the next week." Both of the discarded
alternatives have the same defect: **a shared address bucket combined with an imprecise refund lets
any legitimate sign-in behind that address hand an attacker back one more guess.** "Newest N" pops
whoever the attacker most recently charged, not necessarily this pair's own entries; a /24-wide
"the admin signed in, so this whole neighborhood is clean for a week" grant is a standing invitation
to sign in loudly right after an admin does. An entry a peek-miss charged (`record_address`, used
only by step two's non-consuming peek at an unknown or already-consumed challenge token, D3a) is
tagged `_UNREFUNDABLE` and can never be popped by anyone's success, because it names no account to
match against.

#### D3a — Step two is exactly two hops, with two loop-side verdicts around them (cross-ref ADR 07)

`POST /api/auth/login/verify` cannot ask for the per-pair verdict before it knows which account the
challenge names — but it must not let an unauthenticated caller buy a database connection and a
worker-thread hop for free by posting garbage challenge tokens. `verdict_address` exists for exactly
this: the address tier *alone*, computed from in-memory state on the event loop, with no pair key
and no database read, before hop A. Hop A is a non-consuming *peek* at the challenge (never
`take_login_challenge`, which would burn one of the real user's limited code attempts on a replay of
someone else's stolen token) that reveals the username. Only then is the full pair verdict computed
— on the loop, before hop B — and hop B judges the actual code and mints the session. Two hops,
each preceded by its own loop-side throttle check, is the shape; anything that judged the pair tier
before hop A would need the username the request has not proven it can name yet.

### D4 — The sign-in log records what was attempted, never a credential, and it is evidence, not a gate

`web_auth_log` has no column that can hold a secret: no password, not even its length; no session
token or its hash; no API key, not even a prefix — a presented-but-wrong key is a near miss of a
real one and gets the same treatment as a password. `record_attempt` never raises: a database error
under write contention is caught, rolled back, and logged, and the row is simply not written. That
is deliberate — an audit table must not be able to turn a transient write failure into a locked-out
admin, and nothing in this codebase reads the log to make an access decision, so a missing row costs
nothing but completeness. `EventThrottle` (shared by the rejected-API-key path and the
rate-limited-login path) bounds the log's own write volume to at most one row per address prefix per
minute for each of those two sources, because an attacker who ignores a 429 and keeps sending would
otherwise bury the real password-attempt rows the log exists to show under its own bookkeeping.

### D5 — Session pinning: the auth-cache key change is the load-bearing part

A pinned session (`device` or `device+network` mode) is meant to self-destruct the moment it is
presented by a client it was not issued to. But the auth middleware caches a validated session
verdict for `_AUTH_CACHE_TTL` (30 s) so a page's burst of crop requests does not open a database
connection per image (ADR 07) — and a cache keyed only on the session token would let a stolen
cookie, replayed from the thief's own browser, ride that cached verdict for up to 30 seconds before
the pin is ever re-checked against the database. **The auth-cache key now includes the client facts
— `sessions.cache_key` builds `"s:<sha256(token)>:<sha256(device_key|ip_prefix)>"` — which is the
only thing closing that window.** A different client (different `device_key`, different
`ip_prefix`) simply misses the cache and falls through to `validate_session`, which raises
`SessionPinViolation` and destroys the row on a real mismatch.

That key change has a consequence for the cache's own shape: one token can now own several cache
entries (an honest client that changes network mid-session mints a second key under
`device+network` pinning, for instance), so revocation on logout or a pin change has to sweep every
entry that starts with `cache_prefix(token)`, not pop a single key — `_invalidate_auth_cache` grew a
`prefix=` argument for exactly this. And because a replayed cookie tried against N distinct
User-Agent strings now mints N cache entries instead of one, **the cache must evict rather than grow
without bound**: the insert path in `_auth_lookup` drops every already-expired entry, then pops the
entry with the earliest deadline until the cache is back under `_AUTH_CACHE_MAX` (4096). It is never
`clear()`d wholesale except by the explicit no-argument revocation call used when there is no single
credential to name — a wholesale flush under this kind of attack would convert a memory-growth
problem into a tool for evicting every signed-in user's cached verdict at once, which is strictly
worse than the growth it would be defending against.

`fingerprint` hashes `device_key(user_agent)`, not the raw header, and `ip_prefix(ip)`, not the full
address — both design choices predate this ADR's session-pinning feature only in the sense that
`clientip.py` already needed them for the throttle; pinning is the second consumer. A raw-header pin
would force a full re-authentication (password, and a TOTP code on a 2FA account) on every Chrome or
Edge monthly version bump; a full-address pin under `device+network` would flip on every DHCP lease
renewal on some ISPs. The trade this makes — a thief on the same OS family with a different browser
*version*, from the same /24, now passes a pin check that a byte-exact comparison would have caught
— is accepted because the alternative is a self-inflicted forced sign-out on a schedule the operator
did not choose.

### D6 — Two-step sign-in

**Recovery codes are sha256-hashed, not scrypt-hashed, and this is not an inconsistency with
passwords.** A password is chosen by a human and therefore lives in a space small enough to be
worth slowing down against — scrypt's whole purpose is making each guess expensive. A recovery code
is a `secrets.token_hex(8)` output: 64 bits of CSPRNG, never typed by a human into existence, and
brute-forcing it is astronomically infeasible regardless of hash speed. Hashing it with scrypt would
buy no real resistance and would cost something real: `consume_recovery_code` has to try candidate
codes at code-entry time, and turning that into ten ~100 ms scrypt derivations on the event loop's
threadpool (there are up to twenty live codes) is pure latency for no security benefit. A single
indexed sha256 lookup is the correct tool for a high-entropy, machine-generated secret; scrypt is
the correct tool for a low-entropy, human-chosen one. The two hash functions in this codebase are
doing different jobs, not the same job inconsistently.

**The QR code is drawn in the browser**, by a pinned `qrcode` npm dependency (`TwoStepCard.vue`),
rather than by a hand-written server-side encoder. A correct QR encoder — Reed–Solomon error
correction, mode selection, mask evaluation — is exactly the kind of code nobody should hand-roll
for a security-adjacent feature, and the server's job is limited to producing the `otpauth://` URI
(`totp.provisioning_uri`) and handing it to a battle-tested renderer. The base32 secret is always
shown as plain text beside the QR, because a phone camera that cannot focus, a headless setup over
SSH with a port-forwarded browser, or an authenticator app with no camera support all need the
typed-key path, and it must not be a fallback nobody tested.

**The replay guard lives in the UPDATE's own `WHERE` clause.** A TOTP code is valid for its whole
30-second step and the skew window either side of it, so without a guard the same six digits could
be replayed any number of times inside that window. `twofactor.verify_totp` writes `last_step`
transactionally as part of accepting a code — `UPDATE web_totp SET last_step = ? WHERE user_id = ?
AND (last_step IS NULL OR ? > last_step)` — so the acceptance and the replay check are one atomic
statement, never a read-then-write race in Python that two concurrent requests could both pass. The
clock-rewind escape (`_CLOCK_REWIND_STEPS`, checked when the server's own clock has jumped more than
two hours backwards) exists because a NAS-hosted server without NTP occasionally corrects its clock
backwards, and without the escape every code enrolled or verified before the jump would be
permanently rejected as "already used" once the clock resumed forward past `last_step` again — this
is a distinguishable clock correction, not a replay, and it is treated as one.

`ENROLMENT_TTL` bounds how long an *unconfirmed* `web_totp` row (secret generated, not yet proven
against a real code) survives — `totp_status` reports the remaining time as `pending_expires_in` so
the UI can say "start over" rather than silently failing a confirm attempt against a secret that
expired minutes ago.

### D7 — The `PUT /api/config` guards run inside the threadpool, after pydantic, never on the loop against the running `Settings`

`configio.guarded_write` does all four things — read the file, merge the partial, validate through
`Settings(**merged)`, and run the lockout guards — inside one `run_in_threadpool` closure, after
pydantic has already produced a fully-validated `Settings` object, rather than checking the raw
partial on the event loop and validating separately. Three problems disappear at once by doing it
this way: the "two-save flow" (an operator adds a proxy and an allowlist entry in the same `PUT`,
which has to be judged as one merged, already-valid configuration, not two independent partials each
failing the other's prerequisite); the uncapped-list parse (`ipaddress.ip_network` on operator input
has to run somewhere, and it is CPU/allocation work that does not belong on the loop); and the
type-sniffing that would otherwise be needed to tell "this partial touches `security`" apart from
"this partial's `security` key is a nested dict with different shapes" before `Settings` has had a
chance to normalize it.

**Guard 0 — the environment-shadow refusal — sits in front of the other three guards, and it is not
overridable by `allow_lockout=1`.** `Settings(**merged)` is built from init kwargs, and Pydantic's
own precedence rule (init kwargs beat environment variables, which beat `.env`, which beats TOML)
means that if any `security.*` key is currently set by an environment variable, writing a *different*
value to `config.toml` changes a file nothing reads — the process keeps obeying the environment
variable. Without Guard 0, the later guards would validate and refuse (or accept) a list the running
process will never actually enforce, training an operator to keep re-editing a file that has no
effect. `allow_lockout=1` means "I accept the risk that this write locks me out"; it does not mean
"pretend the environment variable does not exist," so Guard 0 fires regardless of that flag while
Guards 1–3 (the unenforceable-proxy checks and the self-lockout check) are the ones it bypasses.

### D8 — Every shared limiter/log structure carries an explicit `threading.Lock`

This is the first feature set in the web process to touch the same in-memory state from both the
event loop and AnyIO worker threads *in the same request* — a throttle verdict is read on the loop
before a threadpool hop, and recorded from inside that hop once the database work resolves.
`LoginRateLimiter._lock`, `EventThrottle._lock`, `authlog._state_lock`, and `app.py`'s
`auth_cache_lock` are each held for their whole read-modify-write body and never across a database
call, a connection open, or a scrypt derivation — holding a lock across I/O would serialize every
concurrent request behind whichever one happened to be doing the slow part. The failure mode this
prevents is a `RuntimeError` from two threads racing the same sweep-then-evict logic inside the
sign-in path's anti-brute-force component — the one place in the codebase where a crash would be
most disruptive to reproduce and most damaging to hit in production, since it sits directly on the
path an admin needs when they are already having a bad day with their own login.

### D9 — The allowlist covers every request

The gate sits in `_AuthMiddleware`, after the `/api/health` short-circuit but *before* the
`/assets/` bypass and everything else that runs ahead of an auth decision. Placing it any later
would silently exempt the whole SPA bundle from a feature whose own help text promises it covers
"the app bundle" and refuses an address "before it can even see the sign-in page" — an attacker with
a stolen session cookie or API key gets nothing from an address the allowlist refuses, because the
refusal happens before either credential is even inspected. It does not exempt API keys: an
allowlist that only gated the browser login page would be a gate an automation script simply walks
around. `_NoStoreAPI` only stamps `no-store` on `/api/` responses, so the denial page (which serves
plain HTML for a browser hitting a non-API path, and JSON for an API path) stamps its own `no-store`
header directly rather than relying on that middleware — a cached "you are refused" page would
survive a network change that should have let the same browser back in.

**Denying a request costs nothing beyond a dict lookup.** No connection is opened, no worker thread
is taken, no scrypt runs, and nothing is written to `web_auth_log` — a per-denied-request database
write would turn the allowlist itself into a write amplifier for exactly the traffic it exists to
reject. The one exception is a rate-limited warning log line (at most once per 60 seconds per
address, `_denied_log_seen`), so a flood of refused requests produces one line, not one per request.

### D10 — `uvicorn`'s own proxy handling is off, and `proxy_headers` is passed `False` explicitly, never omitted

`serve()` used to call `uvicorn.run(..., proxy_headers=True, forwarded_allow_ips="*")`. That is a
second, independent trust decision layered in front of `clientip.ProxyHeaders`: `forwarded_allow_ips
="*"` believes `X-Forwarded-For` from *any* peer and rewrites `scope["client"]` before Synopticon's
own middleware ever sees the connection — which is precisely what made the per-address login
throttle free to defeat with a random `X-Forwarded-For` value per attempt, since every attempt then
resolved to an address of the attacker's choosing. Omitting `proxy_headers` rather than passing
`False` would not have fixed this: uvicorn's own default is `proxy_headers=True` with
`forwarded_allow_ips` read from the `FORWARDED_ALLOW_IPS` environment variable or defaulted to
`"127.0.0.1"` — which trusts loopback unconditionally, and the documented topology (Synopticon bound
to loopback with nginx on the same host) makes every proxied request arrive from loopback. An
omitted flag would silently re-enable exactly the hazard this ADR exists to close, on the exact
deployment the README recommends, the next time someone reads the uvicorn docs and assumes the
default is safe. `proxy_headers=False` is written explicitly, with a comment saying why, so a future
edit has to notice it and argue with it rather than delete a line that "looks redundant."

### D11 — `allow_private_networks` defaults to `True`, and it governs the allowlist only

An operator who turns the allowlist on to keep the public internet out almost always means "the
public internet," not "my own home network" — defaulting private ranges (RFC1918 + CGNAT +
link-local + unique-local) to always-allowed is what keeps that operator from being locked out of
their own LAN by the same list that was meant to keep strangers out. It has no bearing on login
throttling, which exempts no address (D2); the two features answer different questions ("can this
address load the page at all" versus "has this address tried too many passwords") and this switch
only answers the first one.

### D12 — `GET /api/health` stays exempt (cross-ref ADR 07)

The health probe is checked ahead of the allowlist gate, same as it was already checked ahead of the
auth middleware's threadpool hop before this contract landed — see ADR 07's "Liveness probe"
section, unchanged in spirit. A container orchestrator's healthcheck has no way to present a
listed address if the allowlist is scoped to LAN-only, and a probe that starts failing because the
admin locked down remote access would restart the container out from under whatever job is running,
which is exactly the failure ADR 07 already documents for a probe pointed at a heavier endpoint.

### D13 — The record persists; the punishment does not

`web_auth_log` rows are durable — they are the audit trail, and an operator restoring a database
backup wants their sign-in history back. `LoginRateLimiter` and `EventThrottle` are purely
in-memory and are rebuilt empty on every process restart. This is deliberate, not an oversight: a
throttle is a soft DoS guard, not a security boundary that must survive a crash, and a container
restart has to remain a valid way out of a self-inflicted lockout — a login throttle whose backoff
state persisted across restarts would give an attacker a way to make the admin's own recovery tool
(`docker compose restart`) stop working. The log is therefore explicitly best-effort evidence (D4),
never a ledger a defense consults to decide anything.

### D14 — Account lockout is explicitly rejected, and the deleted global tier was one instance of it

Beyond D2a's global-throttle-tier deletion, this contract as a whole rejects account lockout (a
threshold of failures that disables an *account*, requiring an administrative unlock) as a defense
on a single-admin tool. There is exactly one account that matters on most installs, and a lockout
mechanism whose failure mode is "the one admin account is now locked" has no administrator left to
perform the unlock — every lockout scheme's escape hatch on this project is therefore CLI-only,
requires filesystem access to the box, and is documented as such (`reset-password`, `disable-2fa`,
`web-access --clear`, `session-pin`), never an account-side counter that disables sign-in itself.

### D15 — Every route that changes a security property re-authenticates, session pinning included

Enabling or disabling two-step sign-in, regenerating recovery codes, and changing the
session-pinning mode all require the caller's current password (and, once TOTP is enrolled, a fresh
code or recovery code) inside the same request — `security_routes.py`'s `_require_user` plus each
handler's own `verify_password`/`verify_totp` pair. This is the same shape `configio`'s
change-password route already used before this contract; session pinning follows it precisely
because setting a pin mode is, in one call, both a security-property change *and* a mass session
revocation (`set_pin_mode` deletes every other session of that user and re-pins the caller's own
cookie) — a request that can do that much damage to an account's session state must prove it is
still the account holder typing, not a hijacked, still-valid cookie.

### D16 — The database backup dropped the two-step tables the moment the TOTP secret landed in the schema

`db/snapshot.py::SNAPSHOT_EXCLUDE` (`web_totp`, `web_recovery_codes`, `web_login_challenges`) is
never copied into a downloaded database snapshot. `web_totp.secret` is plaintext base32 — the only
directly usable bearer credential anywhere in the schema, and unlike a password hash there is
nothing to "crack": possessing the row *is* possessing a working second factor, forever, with no
brute-force step in between. Recovery codes are the same credential by another name (a working
bypass of the second factor), and a live login challenge is a session in waiting. None of the three
are removed from `db/copy.py::TABLES`, which stays the full list — `db-migrate` and the PostgreSQL
backend switch share that list and must carry every enrollment forward, or moving backends would
silently un-enroll every account's two-step sign-in. `web_auth_log` is deliberately *not* excluded
from a snapshot: it is evidence, it contains no credential (D4), and an operator restoring a backup
wants their sign-in history intact. Nor is `web_sessions`: its new `pin_hash` column is a sha256
digest of client facts (a User-Agent reduction and an address prefix) that anyone holding the
backup could already observe by other means, not a secret, and a restore is expected to bring live
sessions back rather than silently sign everyone out.

## Consequences

- Nothing in this feature set is conditional on trust, source, or history. A defense that wants an
  exemption has to change this ADR, not add a parameter — `clientip`, `throttle.py` and the
  allowlist all say so in their own module docstrings, and this is the third and last time the
  point gets litigated.
- The login throttle and the sign-in log are two different lifetimes on purpose: one dies with the
  process, the other survives a restart. A future feature that wants "the punishment to survive a
  restart too" is proposing account lockout under a different name and should read D14 first.
- The auth cache's eviction policy (oldest-deadline-first, never wholesale) is now load-bearing for
  session pinning's security property, not just a memory-bound nicety — a future change to that
  cache has to preserve "no single request can evict every other user's cached verdict."
- A reverse proxy that is merely *present* but not correctly configured (D1b) is silently worse than
  no proxy at all for every address-keyed feature in this contract. There is no runtime check for
  this by design; it is a deployment correctness requirement documented at every surface that
  depends on it, and a future contributor tempted to add a heuristic detector should re-read D1b
  before doing so.
- Two-step sign-in, session pinning and the allowlist are all off by default; a fresh install and an
  upgrading one both boot with identical behavior to before this ADR until an operator opts in.
