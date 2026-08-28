# ADR 13 — Backup downloads

**Status:** Accepted
**Applies to:** `web/backup_routes.py`, `db/snapshot.py`, `db/copy.py`, `configio.export_config`, the Utilities page

**Amended by ADR 17.** Migration `0010_web_security.sql` put a plaintext bearer credential into the
schema for the first time (`web_totp.secret`), which this document's snapshot design had not needed
to consider before.

## Context

Everything Synopticon has learned about a library — hours of `extract`, every grouping run, every
review decision, the account you log in with — lives in one database, and everything it needs to
reach the NAS lives in one TOML file. A homelabber who is about to rebuild a container, move to a
bigger disk, or switch the database backend has no way to take either with them from the interface.
The answer today is "shell into the container and find the volume", which is exactly the kind of
thing the web GUI exists to remove.

Three things made this less obvious than "add a download link":

- The database is not always a file. Under PostgreSQL (ADR 09) there is nothing on the Synopticon
  side to hand a browser.
- `config.toml` holds plaintext credentials, and `configio`'s standing rule is that a secret is
  masked to `{"secret": true, "set": bool}` and its plaintext never leaves the process.
- Long-running work in the web process is a job (ADR 06), and jobs produce logs, not files.

## Decision

Two authenticated `GET` downloads under `/api/backup/`, built by a sync handler so FastAPI runs
them on a worker thread and the event loop never sees the work (ADR 07).

### Not jobs

`JOB_SPECS` has no vocabulary for "the product is a file the browser must receive on this
request". A snapshot is a read, it is idempotent, it touches nothing, and its output would be
thrown away by the job runner. Both endpoints are ordinary routes; neither gets a `JOB_SPECS`
entry, so neither is schedulable and neither can be reached by the consent machinery it does not
need.

### The snapshot is always a SQLite file

`db/snapshot.py` produces one artifact shape for both backends:

- **SQLite** — `VACUUM INTO`. One statement, consistent under a reader transaction, compacted on
  the way out, and safe to take while a job is writing.
- **PostgreSQL** — `db/copy.py` replayed in the other direction, into a fresh migrated SQLite file.

`pg_dump` was the obvious alternative and was rejected twice over: the image does not ship it, and
its output only restores into PostgreSQL. A SQLite snapshot restores by being dropped in as
`data/synopticon.db`, or copied back into PostgreSQL with `db-migrate --from` — the same path the
backend switch already uses, already tested.

The cost is honesty about consistency: the PostgreSQL snapshot is a sequence of reads, not one
repeatable-read transaction, so it is not consistent across tables. That is the same guarantee
`db-migrate` gives, and it is documented in the module rather than papered over.

It is built in a `.snapshot-*` working directory under `data_dir` — the volume with room for a
second copy of the database, which a container's `/tmp` usually is not — and removed by the
response's background task. A lock serializes *construction* only and is released before the file
starts streaming, so a client that hangs up mid-download cannot wedge the button; the abandoned
directory is swept by the next build. `/api/backup/database` is excluded from gzip alongside
`/crops`: the payload is mostly incompressible float32, and the compressor runs on the event loop.

### Credentials leave only when asked for, and the ask is recorded

`export_config` hands back `config.toml` verbatim — comments, key order and all, so restoring is a
file copy — with the `SecretStr` fields blanked in place. `?secrets=1` writes them through instead,
behind a checkbox and a confirmation dialog in the UI.

This is the one path in the codebase that serializes a plaintext secret out of the process. The
default keeps `configio`'s rule intact; the opt-in exists because a settings backup that cannot
restore your NAS login is not a settings backup. Both variants land in `audit_log` as
`backup.config` with the `secrets` flag, which makes "a credential left this box" a question the
audit trail can answer. The audit row carries the flag, never the value.

An install configured purely through `SYNOPTICON_*` environment variables has no file to copy; it
gets the effective settings rendered as TOML instead, so the backup is still a usable starting
point.

### A database snapshot must never carry a bearer credential (ADR 17)

`db/snapshot.py::SNAPSHOT_EXCLUDE` (`web_totp`, `web_recovery_codes`, `web_login_challenges`) is
never copied into a downloaded snapshot. `web_totp.secret` is stored plaintext base32 — an
authenticator app needs it in that form, so no amount of hashing on the way in could avoid this —
which makes it the one directly usable bearer credential in the whole schema: unlike a password
hash, there is no cracking step between possessing the row and possessing a working second factor.
Recovery codes are the same credential by another name, and a live login challenge is a session in
waiting. `_snapshot_sqlite` deletes the excluded tables from the `VACUUM INTO` copy (never the
source) and vacuums again so the rows do not merely go invisible in a compacted file;
`_snapshot_postgres` passes the same set to `copy_database`'s new `skip=` parameter.

**This exclusion set is deliberately not `db/copy.py::TABLES`**, which `db-migrate` and the
PostgreSQL backend switch share and which keeps all three tables. A snapshot is a one-way export an
operator might hand to anyone as "my settings, for reference"; a backend migration is the same
database continuing to exist under a different engine, and dropping a live enrolment there would
silently un-enrol every account's two-step sign-in the moment someone switched to PostgreSQL.

`web_auth_log` is deliberately **not** excluded — it is evidence (ADR 17 D4), it contains no
credential in any form, and an operator restoring a backup wants their sign-in history intact. Nor
is `web_sessions`: its `pin_hash` column (ADR 17) is a sha256 digest of client facts a backup
holder could already observe by other means, not a secret, and restoring a database is expected to
bring live sessions back rather than silently sign everyone out.

### Both download routes became user-session-only (ADR 17)

`GET /api/backup/config` and `GET /api/backup/database` now refuse an API-key credential with a 403
explaining that a backup has to come from a signed-in browser. This narrows a surface that was
previously reachable by any identity — a backup is exactly the kind of bulk, sensitive export an
automation script's leaked key should not be able to trigger unattended, and neither route needs the
API-key path for any legitimate use it currently has.

## Consequences

- A backup is a point-in-time read with no consent gate beyond being signed in. It cannot damage
  anything, so it is not in the ADR 05 danger tiers — but it *can* carry credentials off the box,
  which is why the opt-in is explicit and audited rather than a query parameter the UI always sets.
- The snapshot doubles the database's disk footprint on `data_dir` while it is being built and
  downloaded.
- `backup_routes.py` must not carry `from __future__ import annotations` (ADR 08) — it takes
  `Request` parameters.
- Restore stays manual: drop the file in, or `db-migrate --from` it. There is no upload endpoint, and adding
  one would need a real consent gate, since it overwrites the library wholesale.
