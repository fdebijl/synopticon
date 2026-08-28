"""db.store: migration atomicity and concurrency (F6, section 1.2).

`_migrate_sqlite` wraps each migration's DDL and its `PRAGMA user_version`
bump in one `BEGIN IMMEDIATE` transaction, so a crash mid-migration (or two
connections racing to migrate the same file) can never leave a half-applied
migration behind.
"""

from __future__ import annotations

import threading

from synopticon.db import store


def test_concurrent_migration_of_a_fresh_database_applies_exactly_once(tmp_path):
    path = tmp_path / "synopticon.db"
    errors: list[Exception] = []
    versions: list[int] = []
    lock = threading.Lock()

    def worker():
        # sqlite3 connections are thread-affine: open, read and close inside
        # the same worker thread rather than handing the Connection back to
        # the main thread.
        try:
            c = store.connect(path)
            try:
                version = c.execute("PRAGMA user_version").fetchone()[0]
                with lock:
                    versions.append(version)
            finally:
                c.close()
        except Exception as exc:  # noqa: BLE001 - captured for the assertion below
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"duplicate column name or other migration race: {errors!r}"
    assert len(versions) == 8
    assert all(v == len(store._MIGRATIONS) for v in versions)

    c = store.connect(path)
    try:
        names = {
            r["name"]
            for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"web_users", "web_totp", "web_auth_log"} <= names
    finally:
        c.close()


def test_migration_is_atomic_across_a_reconnect(tmp_path):
    path = tmp_path / "synopticon.db"
    c1 = store.connect(path)
    version = c1.execute("PRAGMA user_version").fetchone()[0]
    c1.close()

    # Reconnecting must be a no-op: no re-apply, no version change, no error.
    c2 = store.connect(path)
    assert c2.execute("PRAGMA user_version").fetchone()[0] == version
    c2.close()
