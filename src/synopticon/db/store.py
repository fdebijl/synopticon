"""Database access: connection setup, migrations, blob codecs, sync_state.

Modules own their SQL; this module owns the connection contract — row access,
foreign keys, and schema versioning — plus the choice of backend. SQLite is the
default and needs no configuration; PostgreSQL is opted into via the
``[database]`` config section and the ``[postgres]`` extra.

The SQL itself is written once, in SQLite's dialect, and translated per backend
by :mod:`synopticon.db.dialect`. That includes ``schema.sql`` and every
migration, so there is exactly one schema to keep correct.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from . import postgres as _pg
from .connection import Connection, Cursor
from .dialect import Dialect, PostgresDialect, is_pragma, scan_identity_columns, split_script
from .rows import Row

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Settings

__all__ = [
    "Connection",
    "Cursor",
    "Row",
    "connect",
    "now",
    "vec_to_blob",
    "blob_to_vec",
    "link_photo_id",
    "get_state",
    "set_state",
    "table_exists",
    "describe",
]

_SCHEMA_DIR = Path(__file__).parent
_MIGRATIONS = [
    _SCHEMA_DIR / "schema.sql",  # version 1
    _SCHEMA_DIR / "migrations" / "0002_extract_state.sql",  # version 2
    _SCHEMA_DIR / "migrations" / "0003_photo_hashes.sql",  # version 3
    _SCHEMA_DIR / "migrations" / "0004_photo_hash_index.sql",  # version 4
    _SCHEMA_DIR / "migrations" / "0005_split_merge_named.sql",  # version 5
    _SCHEMA_DIR / "migrations" / "0006_web_auth.sql",  # version 6
    _SCHEMA_DIR / "migrations" / "0007_similar_top_pick.sql",  # version 7
    _SCHEMA_DIR / "migrations" / "0008_schedules.sql",  # version 8
    _SCHEMA_DIR / "migrations" / "0009_widen_integers.pg.sql",  # version 9
    # Future migrations: append db/migrations/000N_*.sql paths here.
]

#: A `.pg.sql` migration applies to PostgreSQL only, and still consumes a version
#: on every backend so the numbering stays shared. It is how a PostgreSQL-only
#: repair (widening int4 columns SQLite always stored as int8) gets expressed
#: without inventing a SQLite dialect for something SQLite never needed.
_PG_ONLY_SUFFIX = ".pg.sql"


def _applies_to(migration: Path, backend: str) -> bool:
    return backend == "postgres" or not migration.name.endswith(_PG_ONLY_SUFFIX)


#: Version bookkeeping on backends without SQLite's `PRAGMA user_version`.
_VERSION_TABLE = "synopticon_schema_version"

_SQLITE_DIALECT = Dialect()
_pg_dialect: PostgresDialect | None = None

#: DSNs this process has already brought up to date. Unlike SQLite's PRAGMA,
#: the PostgreSQL version check is a network round trip, and `connect` runs once
#: per web request — re-asking every time would tax the very path the web
#: layer's responsiveness invariants protect.
_pg_migrated: set[str] = set()
_pg_migrated_lock = threading.Lock()


def _postgres_dialect() -> PostgresDialect:
    global _pg_dialect
    if _pg_dialect is None:
        identity = scan_identity_columns([m.read_text() for m in _MIGRATIONS])
        _pg_dialect = PostgresDialect(identity)
    return _pg_dialect


def _is_postgres_uri(target: Any) -> bool:
    return isinstance(target, str) and target.startswith(("postgresql://", "postgres://"))


def connect(
    target: "Path | str | Settings", check_same_thread: bool = True
) -> Connection:
    """Open a configured connection.

    ``target`` is a :class:`~synopticon.config.Settings` (the normal case — it
    carries the backend choice), or a path/URI for callers that already know
    which database they mean.

    `check_same_thread=False` is only for a connection whose every use is
    already serialized by the caller's own lock (the web process' long-lived
    NAS-session connection, which hops threadpool workers between requests). It
    has no meaning for PostgreSQL, whose connections are not thread-affine.
    """
    database = getattr(target, "database", None)
    if database is not None and database.backend == "postgres":
        return _connect_postgres(_pg.dsn(database), database.pool_size)
    if database is not None:
        target = target.storage.db_path  # type: ignore[union-attr]
    if _is_postgres_uri(target):
        return _connect_postgres(str(target), 5)
    return _connect_sqlite(Path(target), check_same_thread)  # type: ignore[arg-type]


def _connect_sqlite(db_path: Path, check_same_thread: bool) -> Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(db_path, timeout=60, check_same_thread=check_same_thread)
    raw.row_factory = sqlite3.Row
    _set_wal_mode(raw)
    raw.execute("PRAGMA foreign_keys=ON")
    raw.execute("PRAGMA synchronous=NORMAL")
    conn = Connection(raw, _SQLITE_DIALECT)
    _migrate_sqlite(conn)
    return conn


def _set_wal_mode(raw: sqlite3.Connection) -> None:
    """Switch a fresh connection to WAL, retrying past a same-instant collision.

    Converting journal mode takes an exclusive lock, and unlike an ordinary
    write it does not go through the busy handler that ``timeout=`` installs —
    SQLite reports ``SQLITE_BUSY`` immediately instead of waiting. Several
    ``store.connect`` calls landing at once (a fresh file's first boot) would
    otherwise fail here before the migration lock in ``_migrate_sqlite`` is
    ever reached.
    """
    deadline = time.monotonic() + 60
    while True:
        try:
            raw.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as exc:
            # Only contention is worth waiting out. A read-only file or a
            # missing directory raises here too, and retrying those for a
            # minute turns an immediate, legible error into a hang.
            if "locked" not in str(exc) and "busy" not in str(exc).lower():
                raise
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def _connect_postgres(conninfo: str, pool_size: int) -> Connection:
    raw, release = _pg.acquire(conninfo, pool_size)
    # `reopen` is what lets a multi-hour command outlive a database restart: the
    # wrapper re-acquires from the pool instead of ending the run. Migrations are
    # not re-checked, `_pg_migrated` having already recorded this DSN.
    conn = Connection(
        raw,
        _postgres_dialect(),
        release,
        reopen=lambda: _pg.acquire(conninfo, pool_size),
    )
    if conninfo not in _pg_migrated:
        _migrate_postgres(conn, conninfo)
    return conn


def _migrate_sqlite(conn: Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= len(_MIGRATIONS):
        # Already current: return without a commit. The web GUI opens a
        # connection per request, and an unconditional commit here costs a
        # journal write + lock round-trip on every one of them.
        return
    for i, migration in enumerate(_MIGRATIONS, start=1):
        if i <= version:
            continue
        # The DDL and the version bump are one atomic unit, and the transaction
        # is IMMEDIATE, not deferred. A crash between two ALTERs (or a bare
        # executescript, which issues its own implicit COMMIT before running --
        # verified against Python 3.11's sqlite3, which is why the body is run
        # statement by statement instead) would otherwise leave a half-applied
        # migration that replays into "duplicate column name" -- unbootable, and
        # every recovery command comes back through here. And a *deferred* BEGIN
        # takes no write lock until the first write, so two connections could
        # both pass the version check above and the loser would hit the same
        # error; store.connect runs once per web request, and the first boot
        # after an upgrade is when several land at once. BEGIN IMMEDIATE takes
        # the write lock up front, and the re-read below is what makes the
        # winner's bump visible to the loser. PRAGMA user_version IS
        # transactional, so it rolls back with the body.
        body = migration.read_text() if _applies_to(migration, "sqlite") else ""
        try:
            conn.execute("BEGIN IMMEDIATE")
            # Re-read inside the transaction: another connection may have
            # applied this migration between our check above and our lock.
            if conn.execute("PRAGMA user_version").fetchone()[0] >= i:
                conn.execute("COMMIT")
                continue
            for statement in split_script(body):
                if not is_pragma(statement):
                    conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {i}")
            conn.execute("COMMIT")
        except Exception:
            conn.rollback()
            raise
    conn.commit()


def _migrate_postgres(conn: Connection, conninfo: str) -> None:
    with _pg_migrated_lock:
        if conninfo in _pg_migrated:
            return
        if _pg_schema_version(conn) >= len(_MIGRATIONS):
            _pg_migrated.add(conninfo)
            conn.rollback()
            return
        # Slow path only. A session-level advisory lock (not transaction-level:
        # applying migrations commits) serializes the web server and any job
        # subprocess that starts at the same moment.
        conn.execute("SELECT pg_advisory_lock(?)", (_pg.MIGRATION_LOCK_KEY,))
        try:
            version = _pg_schema_version(conn)
            for i, migration in enumerate(_MIGRATIONS, start=1):
                if i > version and _applies_to(migration, "postgres"):
                    conn.executescript(migration.read_text())
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS {_VERSION_TABLE} (version INTEGER NOT NULL)"
            )
            conn.execute(f"DELETE FROM {_VERSION_TABLE}")
            conn.execute(
                f"INSERT INTO {_VERSION_TABLE} (version) VALUES (?)", (len(_MIGRATIONS),)
            )
            conn.commit()
        except Exception:
            # A failed statement poisons the transaction; the unlock below would
            # fail too if we did not get out of it first.
            conn.rollback()
            raise
        finally:
            conn.execute("SELECT pg_advisory_unlock(?)", (_pg.MIGRATION_LOCK_KEY,))
            conn.commit()
        _pg_migrated.add(conninfo)


def _pg_schema_version(conn: Connection) -> int:
    """Current schema version, or 0 when the database is empty.

    Uses ``to_regclass`` rather than catching the error from selecting out of a
    missing table: in PostgreSQL a failed statement poisons the whole
    transaction, so the recovery would cost a rollback anyway.
    """
    exists = conn.execute("SELECT to_regclass(?)", (_VERSION_TABLE,)).fetchone()
    if exists is None or exists[0] is None:
        return 0
    row = conn.execute(f"SELECT version FROM {_VERSION_TABLE}").fetchone()
    return int(row[0]) if row else 0


def describe(settings: "Settings") -> str:
    """Name the configured database for a human, never including credentials."""
    database = settings.database
    if database.backend != "postgres":
        return str(settings.storage.db_path)
    if database.url.get_secret_value().strip():
        return "postgresql (connection URL)"
    return f"postgresql://{database.host}:{database.port}/{database.database}"


def table_exists(conn: Connection, name: str) -> bool:
    """Whether ``name`` is a table in the connected database."""
    if conn.dialect.name == "sqlite":
        sql = "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?"
    else:
        sql = "SELECT 1 FROM information_schema.tables WHERE table_name=?"
    return conn.execute(sql, (name,)).fetchone() is not None


def now() -> int:
    return int(time.time())


def vec_to_blob(vec: np.ndarray) -> bytes:
    return np.ascontiguousarray(vec, dtype=np.float32).tobytes()


def blob_to_vec(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def link_photo_id(conn: Connection, space: str, photo_id: int) -> int:
    """Resolve a photo id to link-build against: its similar-group top_pick if set.

    Non-top-pick members of a Synology "similar photo group" have no timeline
    route of their own (the grouped view only shows the top pick), so deep
    links must target the group's top_pick id instead. Returns `photo_id`
    unchanged for ungrouped photos or if the row is missing.
    """
    row = conn.execute(
        "SELECT similar_top_pick FROM photos WHERE space = ? AND id = ?",
        (space, photo_id),
    ).fetchone()
    if row is None or row["similar_top_pick"] is None:
        return photo_id
    return int(row["similar_top_pick"])


def get_state(conn: Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value_json FROM sync_state WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value_json"]) if row else default


def set_state(conn: Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO sync_state (key, value_json) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json",
        (key, json.dumps(value)),
    )
    conn.commit()
