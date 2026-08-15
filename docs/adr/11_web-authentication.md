# ADR 11 — Stdlib-only web authentication

**Status:** Accepted
**Applies to:** `web/auth.py`, `web/app.py`, migration `0006_web_auth.sql`, `cli.py`'s `reset-password`

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
| `web_users` | scrypt password with a per-user salt, compared with `hmac.compare_digest` |
| `web_sessions` | 256-bit opaque token stored hashed; HttpOnly + SameSite=Lax cookie, 30-day, `Secure` when the request scheme is https via `--proxy-headers` |
| `web_api_keys` | `syn_<32hex>`, stored sha256-hashed, named and revocable, sent as `Authorization: Bearer` |

### The API is JSON — no HTML form or redirect login

- `POST /api/auth/login` — `{username, password}` → 200 plus session cookie, 401, or 429 via
  `LoginRateLimiter`
- `POST /api/auth/logout`
- `GET /api/auth/me` — `{authenticated, username, first_boot, version}`, returns 200 even when
  unauthenticated, and is allowlisted in both middleware branches so the Vue Router guard can gate
  client-side
- `POST /api/auth/create-account`

Middleware behaviour: an unauthenticated page request → 302 `/login`; an unauthenticated `/api/*`
→ 401.

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

It needs filesystem access to the database but no login. **It must never gain a `JOB_SPECS`
entry** (ADR 05): a web job would let a session that is already authenticated rewrite the
credential.

## Consequences

- Session validation is cached in the auth middleware for 30 s; API keys are deliberately not
  cached, so key revocation stays exact. See ADR 07 for why.
- scrypt is ~100 ms of deliberate CPU per call, which is why every path that runs it is
  threadpooled.
