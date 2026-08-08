"""Thin SQLite wrapper: connection setup, migrations, blob codecs, sync_state.

Modules own their SQL; this module owns the connection contract:
WAL, foreign keys, Row factory, and schema versioning via PRAGMA user_version.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import numpy as np

_SCHEMA_DIR = Path(__file__).parent
_MIGRATIONS = [
    _SCHEMA_DIR / "schema.sql",  # version 1
    _SCHEMA_DIR / "migrations" / "0002_extract_state.sql",  # version 2
    _SCHEMA_DIR / "migrations" / "0003_photo_hashes.sql",  # version 3
    _SCHEMA_DIR / "migrations" / "0004_photo_hash_index.sql",  # version 4
    _SCHEMA_DIR / "migrations" / "0005_split_merge_named.sql",  # version 5
    _SCHEMA_DIR / "migrations" / "0006_web_auth.sql",  # version 6
    _SCHEMA_DIR / "migrations" / "0007_similar_top_pick.sql",  # version 7
    # Future migrations: append db/migrations/000N_*.sql paths here.
]


def connect(db_path: Path | str, check_same_thread: bool = True) -> sqlite3.Connection:
    """Open a configured connection.

    `check_same_thread=False` is only for a connection whose every use is
    already serialized by the caller's own lock (the web process' long-lived
    NAS-session connection, which hops threadpool workers between requests).
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=60, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= len(_MIGRATIONS):
        # Already current: return without a commit. The web GUI opens a
        # connection per request, and an unconditional commit here costs a
        # journal write + lock round-trip on every one of them.
        return
    for i, migration in enumerate(_MIGRATIONS, start=1):
        if i > version:
            conn.executescript(migration.read_text())
            conn.execute(f"PRAGMA user_version = {i}")
    conn.commit()


def now() -> int:
    return int(time.time())


def vec_to_blob(vec: np.ndarray) -> bytes:
    return np.ascontiguousarray(vec, dtype=np.float32).tobytes()


def blob_to_vec(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def link_photo_id(conn: sqlite3.Connection, space: str, photo_id: int) -> int:
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


def get_state(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value_json FROM sync_state WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value_json"]) if row else default


def set_state(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO sync_state (key, value_json) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json",
        (key, json.dumps(value)),
    )
    conn.commit()
