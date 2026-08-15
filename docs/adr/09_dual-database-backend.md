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

### Translation is a character scanner, not a regex

`_chunks` scans character by character, because a `?` inside a string literal is data and a `;`
inside one is not a statement boundary.

DDL rules:

| SQLite | PostgreSQL | Note |
|---|---|---|
| `INTEGER PRIMARY KEY AUTOINCREMENT` | identity column | |
| `BLOB` | `BYTEA` | |
| `REAL` | `DOUBLE PRECISION` | PostgreSQL `REAL` is float4, and face bboxes take part in a `UNIQUE` key — narrowing them would collide distinct detections |
| `json_extract` | a `jsonb` path | |

Comments are dropped before any of it.

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
- `test_postgres_backend.py` is the real round trip — schema application, upserts, `RETURNING`,
  blob/float fidelity, error recovery, the copy, and the web API. It skips unless
  `SYNOPTICON_TEST_POSTGRES_DSN` points at a throwaway server, and it drops and recreates `public`
  per test, which re-proves the migrations apply from nothing on every run.

## Consequences

- A new migration is appended to `_MIGRATIONS` in `db/store.py`, written in SQLite's dialect, and
  needs no PostgreSQL counterpart.
- Credential fields in `DatabaseConfig` must be `SecretStr`, never plain `str`, or `configio`'s
  masking does not cover them.
