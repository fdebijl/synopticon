# ADR 11 — Stdlib-only web authentication

**Status:** Accepted
**Applies to:** `web/auth/*`, `web/clientip.py`, `web/totp.py`, `web/security_routes.py`, `web/app.py`, migrations `0006_web_auth.sql`/`0010_web_security.sql`, `cli.py`'s `reset-password`/`disable-2fa`/`web-access`/`session-pin`

**Amended by ADR 17** — two-step sign-in, session pinning, a network allowlist, a sign-in log and
login throttling all landed on this surface together; ADR 17 carries the reasoning, this document
carries the resulting shape. `web/auth.py` also split into the `web/auth/` package during that
pass — every public name re-exports unchanged, so nothing below that names `auth.X` needed to move.

## Context

The GUI can write to a NAS photo library, so it needs authentication. But Synopticon is a homelab
tool that people self-host, and every auth dependency added is another CVE feed to track and
another thing that can break an upgrade on a machine nobody watches.

The GUI also has to be usable on first boot, when no account exists yet, without leaving a window
where an unauthenticated stranger can claim the instance.

## Decision

Session cookies plus API keys, implemented with the standard library only — no auth dependencies.

### Storage

| Table | Contents |
|---|---|
| `web_users` | scrypt password with a per-user salt, compared with `hmac.compare_digest`; `session_pin_mode` (ADR 17) |
| `web_sessions` | 256-bit opaque token stored hashed; HttpOnly + SameSite=Lax cookie, 30-day, `Secure` when the effective scheme is https (resolved by `clientip`, honoured only from a listed `trusted_proxies` address — ADR 17 D1/D10); `pin_mode`/`pin_hash` (ADR 17) denormalise the enforcement datum onto the row itself |
| `web_api_keys` | `syn_<32hex>`, stored sha256-hashed, named and revocable, sent as `Authorization: Bearer` |
| `web_totp` (ADR 17) | one row per user: base32 secret (plaintext — the only directly usable bearer credential in the schema), `confirmed_at`, `last_step` (the replay guard) |
| `web_recovery_codes` (ADR 17) | single-use backup codes, sha256-hashed (not scrypt — see ADR 17 D6) |
| `web_login_challenges` (ADR 17) | a half-finished sign-in waiting on its second-step code; `user_id` is `NULL` for a wrong-password challenge so the response is byte-identical to a right one (ADR 17 D3) |
| `web_auth_log` (ADR 17) | best-effort sign-in history: what was attempted, never a credential |

### The API is JSON — no HTML form or redirect login

- `POST /api/auth/login` — `{username, password}` → 200 plus session cookie, 401, 429 via
  `LoginRateLimiter`, or — on an instance where any account has two-step sign-in enrolled —
  `{mfa_required: true, challenge, expires_in}` with **no cookie**, whether the password was right
  or wrong (ADR 17 D3's oracle-avoidance)
- `POST /api/auth/login/verify` — `{challenge, code}` → 200 plus session cookie, 401, or 429; the
  second step of the flow above, reachable with no session of its own (see ADR 07's amendment for
  its two-hop shape)
- `POST /api/auth/logout`
- `GET /api/auth/me` — `{authenticated, username, first_boot, version, totp_enabled,
  session_pinning}`, returns 200 even when unauthenticated, and is allowlisted in both middleware
  branches so the Vue Router guard can gate client-side. **`totp_enabled` and `session_pinning` are
  `None` unless `authenticated` is true, and always describe the caller's own account** — this route
  answers 200 to anybody, and "does anyone here have a second factor" would be a direct invitation
  on a single-admin instance if it answered for anyone else
- `POST /api/auth/create-account`

Eleven more routes landed with ADR 17, all defined in `security_routes.py` and all requiring a
signed-in user (an API key reaches none of them — managing a factor, a pin or the instance's
protection state needs a human who just re-typed a password):

| Route | Purpose |
|---|---|
| `GET /api/auth/totp` | This account's two-step status: enrolled, pending, recovery codes remaining |
| `POST /api/auth/totp/start` | Begin enrolment (password re-auth) — returns the secret, QR URI and manual-entry groups |
| `POST /api/auth/totp/confirm` | Prove the authenticator app works; issues recovery codes, revokes every other session |
| `POST /api/auth/totp/disable` | Turn two-step sign-in off (password + code re-auth); revokes every other session |
| `POST /api/auth/totp/recovery-codes` | Regenerate the backup code set (password + code re-auth); revokes every other session |
| `GET /api/auth/session-pinning` | This account's pinning mode plus what the calling browser currently looks like |
| `POST /api/auth/session-pinning` | Change the pinning mode (password + code re-auth); re-pins the caller's cookie, revokes every other session |
| `GET /api/security/access` | The resolved client address, the allowlist's verdict, and the proxy-trust diagnostics |
| `GET /api/security/log` | Paginated, filterable sign-in log |
| `GET /api/security/throttles` | A snapshot of the in-memory throttle state |
| `POST /api/security/throttles/clear` | Manually clear a throttled address or username pair |

Middleware behaviour: an unauthenticated page request → 302 `/login`; an unauthenticated `/api/*`
→ 401 (or, for an address refused by the network allowlist, 403 before either credential is even
inspected — ADR 17 D9); a session presented by a client it was not pinned to → the pin-specific 401
or a redirect to `/login?reason=pin`, with the cookie dropped either way.

**Every mutating `/api/*` request must carry `Content-Type: application/json`.** That is the CSRF
defence — a cross-origin form post cannot set it. The SPA's `api/client.ts` sends it on all
mutations, including body-less ones (logout, cancel, revoke).

### First-boot claim

With no users in the database, only `/setup`, `/api/setup/*`, `/api/auth/create-account` and
`/api/auth/me` are reachable. Everything else 302s to `/setup` until the single admin account is
created.

`have_users` is a one-way latch, since no route deletes users — which is also what makes the
anonymous short-circuit in ADR 07 safe.

### Lockout recovery is CLI-only

`synopticon reset-password [USERNAME]` calls `auth.change_password` plus
`auth.delete_user_sessions`, which revokes that user's cookies unless `--keep-sessions`.

ADR 17 added three more CLI-only recovery commands, one per way this surface can lock its own admin
out:

- `synopticon disable-2fa [USERNAME]` — the recovery path for a lost authenticator device: drops
  the confirmed factor and its recovery codes, discards any half-finished sign-in waiting on a code,
  and revokes every session of that account.
- `synopticon session-pin off [USERNAME]` — the recovery path for a pin loop that keeps signing a
  legitimate browser out: clears the setting and unpins every existing session *in place*, without
  signing anyone out. (Only `off` is settable from the shell — `device`/`device+network` pin a
  session to the browser that requests them, and the server has no way to know which browser that
  should be from a shell prompt.)
- `synopticon web-access --clear` — the recovery path for a network allowlist that locked out the
  browser trying to fix it: empties `[security] allow_from`. **This is the only one of the four that
  needs a restart** — the allowlist is read once at start-up, same as `trusted_proxies` and
  `allow_private_networks`, while a password, a factor and a pin all take effect on the very next
  request because they are read from the database on every request, not cached at process start.

All four need filesystem access to the database (or, for `web-access`, the config file) but no
login. **None of the four may ever gain a `JOB_SPECS` entry** (ADR 05): a web job would let a
session that is already authenticated rewrite the very credential, factor, pin or list it exists to
recover from.

## Consequences

- Session validation is cached in the auth middleware for 30 s; API keys are deliberately not
  cached, so key revocation stays exact. See ADR 07 for why — and, since ADR 17, for why the cache
  key now folds in the client facts too.
- scrypt is ~100 ms of deliberate CPU per call, which is why every path that runs it is
  threadpooled.
- Everything ADR 17 added to this surface — two-step sign-in, session pinning, the network
  allowlist, the sign-in log, login throttling — is off by default, so an upgrade changes no
  existing install's behaviour until an operator opts in.
