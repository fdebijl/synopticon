"""Audit trail over `audit_log`: every write attempt (and dry-run) is recorded."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from synopticon.db import store


def record(
    conn: sqlite3.Connection,
    action: str,
    api: str | None = None,
    params: dict[str, Any] | None = None,
    response: dict[str, Any] | None = None,
    success: bool | None = None,
    review_item_id: int | None = None,
) -> int:
    """Insert one `audit_log` row; returns its id."""
    cur = conn.execute(
        "INSERT INTO audit_log (ts, action, api, params_json, response_json, success, review_item_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            store.now(),
            action,
            api,
            json.dumps(params, default=str) if params is not None else None,
            json.dumps(response, default=str) if response is not None else None,
            None if success is None else int(bool(success)),
            review_item_id,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def tail(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    """Most recent `limit` audit_log rows, newest first."""
    return conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
