# ADR 09 — One SQL dialect, two database backends

**Status:** Accepted
**Applies to:** `db/`, `db/schema.sql`, every call-site query

## Context

SQLite is the right default for a homelab tool: no configuration, one file, no server. But users
running Synopticon alongside an existing PostgreSQL instance wanted to use it, and the web process
opens a connection per request, so a naive port would have been slow as well as forked.

The obvious failure mode is a codebase where every query exists twice, and one of the two copies is
always the stale one.

## Decision

Author all SQL once, in SQLite's dialect, and translate at the driver boundary.

SQLite (`data/synopticon.db`) is the default and needs no configuration. PostgreSQL is selected by
`[database] backend = "postgres"` and needs the `[postgres]` extra (`psycopg[binary,pool]`).

MySQL/MariaDB are deliberately out of scope: no `ON CONFLICT`, and `TEXT` cannot be a key column.
Either would fork the schema, which is the exact thing this decision exists to prevent.

### Write SQL in SQLite's dialect, always

`schema.sql`, every migration, and all ~140 call-site queries are authored once, in SQLite's
dialect, with `?` placeholders. `db/dialect.py` translates per backend.

The upsert form every sync path uses — `ON CONFLICT (cols) DO UPDATE SET x = excluded.x` — is
byte-identical in both, which is why no upsert needed rewriting.

**Never add a SQLite-only function.** `json_extract` is translated; `strftime`, `printf` and
`group_concat` are not. Do that work in Python instead.

DML rules:

| SQLite | PostgreSQL | Note |
|---|---|---|
| `?` | `%s` | a literal `%` is doubled, but only when binding — psycopg skips placeholder parsing for a parameterless statement |
| `a IS NOT b` | `a IS DISTINCT FROM b` | SQLite spells null-safe comparison with `IS`; PostgreSQL has no such form and raised a bare `syntax error at or near "p"` on `extract`'s cache-key filter. The unary predicates (`IS NULL`, `IS NOT TRUE`, …) are left alone |
| `a IS b` | `a IS NOT DISTINCT FROM b` | the same operator, negated |
| `json_extract` | a `jsonb` path | DDL only, for generated columns and indexes |

### Translation is a character scanner, not a regex

`_chunks` scans character by character, because a `?` inside a string literal is data and a `;`
inside one is not a statement boundary.

DDL rules:

| SQLite | PostgreSQL | Note |
|---|---|---|
| `INTEGER PRIMARY KEY AUTOINCREMENT` | identity column | |
| `INTEGER` | `BIGINT` | SQLite's INTEGER is int8; PostgreSQL's is int4. `photos.indexed_time` is epoch *milliseconds* (~1.8e12), so int4 overflowed on the first row `db-migrate` copied |
| `BLOB` | `BYTEA` | |
| `REAL` | `DOUBLE PRECISION` | PostgreSQL `REAL` is float4, and face bboxes take part in a `UNIQUE` key — narrowing them would collide distinct detections |

Comments are dropped before any of it.

### A migration may be scoped to one backend

`_MIGRATIONS` entries ending in `.pg.sql` apply to PostgreSQL only, and still consume a schema
version everywhere so the numbering stays shared. There is exactly one, and the reason it exists
is the `INTEGER` → `BIGINT` row above: the translator fixes a *fresh* database, but a PostgreSQL
library created before that fix carries int4 columns that no amount of translation reaches, so
`0009_widen_integers.pg.sql` alters them in place. SQLite has no half to write — its INTEGER was
64-bit all along, and `ALTER COLUMN ... TYPE` is not syntax it accepts.

The widening list is cross-checked against the `INTEGER` columns declared by migrations 1-8
(`test_db_dialect.py::TestIntegerWidening`), because a column missing from it stays capped at
2.1e9 on upgraded installs only — the hardest kind of divergence to notice.

### `cur.lastrowid` works on both

PostgreSQL has no `lastrowid`, so `Connection.execute` appends `RETURNING <pk>` to inserts into
identity tables and reads the value back.

The table→column map is *scanned out of the migration DDL* (`scan_identity_columns`), not
hand-maintained, so a new autoincrement table needs no change. `test_db_dialect.py` asserts the
full expected map.

### The wrapper preserves `sqlite3`'s contract exactly

In `db/connection.py` and `db/rows.py`: `conn.execute(sql, params)` returns a cursor, and a row
indexes by name *and* position, iterates **values** (`tuple(row)` — `lookups.fingerprint` depends
on this), and answers `keys()` / `dict(row)`.

A row is therefore **not** a `collections.abc.Mapping`. That would iterate keys and silently turn
`fingerprint()` into a tuple of column names — a cache key that never changes.

`with conn:` is deliberately unimplemented: `sqlite3` gives it commit/rollback rather than close
semantics, and no call site uses it.

Driver exceptions never escape the wrapper. They arrive as `db.errors.DatabaseError`,
`IntegrityError` or `OperationalError`.

### Catching a database error means rolling back

This is the one real semantic difference between the backends. SQLite shrugs off a failed
statement; PostgreSQL aborts the entire transaction, so every later statement on that connection
fails too.

The four recovery sites — `web/auth.create_user`, `review/lookups.fingerprint`,
`ops_routes._count`, `setup_routes._count` — all call `rollback()` before continuing, which is a
no-op on SQLite. **Any new `except db_errors.*` that keeps using the connection must do the same.**

### A lost session reconnects itself, at a transaction boundary only

A file cannot hang up. A network database can, and a batch command is the worst case for it: one
connection held for hours, with minutes of CPU-bound detection between two statements. An `extract`
run died at photo 25 of 500 that way — `server closed the connection unexpectedly` on the first
statement after a gap, and then `rollback()` in the per-photo handler raised `the connection is
lost` on top of it, which escaped the handler that exists so *one bad photo must not abort the run*.

Three parts, all inside `db/`:

- **Keepalives are set, because libpq does not set them.** libpq defers TCP keepalives to the OS and
  Linux waits two hours before its first probe, which is ample time for a NAT table or a stateful
  firewall to forget an idle session. `postgres.connect_kwargs` layers `keepalives_idle=30` under
  the DSN, and anything the user's own connection string states explicitly wins.
- **`rollback()` is silent when the session is already gone.** There is nothing left to roll back —
  the server discarded the transaction when the socket died — and raising there would displace the
  error the caller is already handling.
- **`Connection` re-acquires from the pool and replays the statement — but only when the current
  transaction has written nothing.** This is the load-bearing half. A dropped socket rolls the whole
  transaction back server-side, so replaying a statement that followed earlier writes would commit a
  *fragment* of a transaction. `_dirty` tracks whether anything in this transaction may have
  written; past that point the transaction is the caller's to redo, and the batch loops already roll
  back and skip per item, so the statement after that rollback is the one that reconnects.

**Do not widen the replay rule to every statement.** Silent partial transactions are a far worse
failure than a run that stops with a clear error, and nothing in the codebase retries a *transaction*
automatically. `_RECONNECT_PAUSES` bounds the wait; after that the mapped `OperationalError` reaches
the caller as before.

SQLite passes no `reopen`, so none of this changes its behaviour.

### Schema versioning differs, migrations do not

SQLite uses `PRAGMA user_version`, unchanged, so existing databases are untouched.

PostgreSQL uses a `synopticon_schema_version` table plus a **session** advisory lock, taken only on
the slow path, so a web server and a job subprocess starting together cannot both migrate. The
version check result is latched per DSN per process (`_pg_migrated`), because `store.connect` runs
*per web request* and the check is a network round trip.

### Pooling is not optional under PostgreSQL

`web/app.py` opens a connection per request. Unpooled, that is a TCP round trip plus auth on every
dashboard poll, which breaks the ADR 07 responsiveness invariants outright.

`db/postgres.py` keeps one `psycopg_pool.ConnectionPool` per DSN per process, and
`Connection.close()` is what returns the connection — **every caller must close**. It rolls back
first: psycopg opens a transaction on the first statement of *any* kind, so a read-only request
would otherwise hand back a connection still holding a snapshot.

### `store.connect(settings)` is the normal call

`Settings` carries the backend choice. A `Path` or URI still works for callers that already know
which database they mean (tests, `db-migrate --from`).

`store.describe(settings)` names the database for a human without leaking credentials. Use it
instead of printing `storage.db_path`.

### `db-migrate` is CLI-only

`db/copy.py` rewrites the destination wholesale, so like `eval` and `reset-password` it must never
gain a `JOB_SPECS` entry (ADR 05).

It copies `TABLES` in FK-safe order, preserves primary keys verbatim, refuses a non-empty
destination, and then `setval`s every identity sequence past the copied ids — skip that and the
next insert collides.

## Testing

- `test_db_dialect.py` covers translation with no database, and always runs.
- `test_db_connection.py` covers the reconnect and replay-safety rules against a stub driver that
  can go bad on command, so a lost session is tested without a server to unplug. It always runs.
- `test_postgres_backend.py` is the real round trip — schema application, upserts, `RETURNING`,
  blob/float fidelity, error recovery, the copy, and the web API. It skips unless
  `SYNOPTICON_TEST_POSTGRES_DSN` points at a throwaway server, and it drops and recreates `public`
  per test, which re-proves the migrations apply from nothing on every run.

## Consequences

- A new migration is appended to `_MIGRATIONS` in `db/store.py`, written in SQLite's dialect, and
  needs no PostgreSQL counterpart.
- Credential fields in `DatabaseConfig` must be `SecretStr`, never plain `str`, or `configio`'s
  masking does not cover them.
- A long-running command survives a database restart, but a transaction interrupted mid-write is
  lost — which is fine only because every phase is resumable and each item is one transaction. A new
  batch loop must keep that shape: commit per item, and roll back before continuing.
