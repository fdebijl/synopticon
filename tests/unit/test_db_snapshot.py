"""db/snapshot.py's SNAPSHOT_EXCLUDE (§1.3, W1).

Pins the two mechanisms apart: `copy.TABLES` is what `db-migrate` and the
backend switch use and must carry every enrolment forward, while
`db/snapshot.py`'s `SNAPSHOT_EXCLUDE` is the snapshot-only filter that keeps a
plaintext `web_totp.secret` out of a backup download. The natural "fix" for
one is to edit the other, and it is the wrong one -- these tests exist so that
mistake fails loudly.

Hermetic -- no NAS, no PostgreSQL server. `copy_database`'s non-SQLite branch
is backend-agnostic over two `Connection`s, so a second SQLite file stands in
for it here exactly as `test_web_backup.py`'s PostgreSQL test does.
"""

from __future__ import annotations

import sqlite3

import pytest

from synopticon.config import load_settings
from synopticon.db import copy as db_copy
from synopticon.db import snapshot as db_snapshot
from synopticon.db import store
from synopticon.web import auth
from synopticon.web import totp as totp_codes
from synopticon.web.auth import twofactor


def _enrol(conn, username="alice"):
    """Create a user with a confirmed TOTP factor and one recovery-code set."""
    uid = auth.create_user(conn, username, "hunter22")
    pending = twofactor.start_totp_enrolment(conn, uid, now=1000)
    code = totp_codes.code_for(pending.secret, totp_codes.current_step(1000))
    codes = twofactor.confirm_totp_enrolment(conn, uid, code, now=1000)
    assert codes  # sanity: enrolment actually confirmed
    return uid


def test_snapshot_excludes_totp_tables_but_keeps_the_user(tmp_path):
    settings = load_settings(storage={"data_dir": tmp_path})
    conn = store.connect(settings.storage.db_path)
    uid = _enrol(conn)
    conn.close()

    dest = tmp_path / "backup.db"
    db_snapshot.snapshot(settings, dest)

    raw = sqlite3.connect(dest)
    try:
        user_row = raw.execute(
            "SELECT id, username FROM web_users WHERE id = ?", (uid,)
        ).fetchone()
        assert user_row == (uid, "alice")

        assert raw.execute("SELECT COUNT(*) FROM web_totp").fetchone() == (0,)
        assert raw.execute("SELECT COUNT(*) FROM web_recovery_codes").fetchone() == (0,)
    finally:
        raw.close()


def test_snapshot_excludes_totp_tables_even_when_absent_in_the_source(tmp_path, monkeypatch):
    """A source that predates migration 10 has no web_totp table at all --
    the exclusion must not choke on that (`db-migrate` always migrates first,
    but a stray old file must not turn a backup download into a 500)."""
    settings = load_settings(storage={"data_dir": tmp_path})
    conn = store.connect(settings.storage.db_path)
    conn.execute("DROP TABLE web_recovery_codes")
    conn.execute("DROP TABLE web_totp")
    conn.commit()
    conn.close()

    dest = tmp_path / "backup.db"
    db_snapshot.snapshot(settings, dest)  # must not raise

    raw = sqlite3.connect(dest)
    try:
        names = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "web_totp" not in names
    finally:
        raw.close()


def test_copy_database_with_no_skip_still_copies_both_tables(tmp_path):
    """`db-migrate` and the backend switch call `copy_database` with no `skip`
    -- they must keep carrying every enrolment forward."""
    source_settings = load_settings(storage={"data_dir": tmp_path / "src"})
    source = store.connect(source_settings.storage.db_path)
    _enrol(source)

    target = store.connect(tmp_path / "target.db")
    try:
        counts = db_copy.copy_database(source, target)
    finally:
        source.close()
        target.close()

    assert counts.get("web_totp") == 1
    assert counts.get("web_recovery_codes") == 10

    target2 = store.connect(tmp_path / "target.db")
    try:
        assert target2.execute("SELECT COUNT(*) AS n FROM web_totp").fetchone()["n"] == 1
        assert (
            target2.execute("SELECT COUNT(*) AS n FROM web_recovery_codes").fetchone()["n"] == 10
        )
    finally:
        target2.close()


def test_copy_database_with_snapshot_exclude_skip_drops_both_tables(tmp_path):
    """The mechanism `db/snapshot.py._snapshot_postgres` actually uses."""
    source_settings = load_settings(storage={"data_dir": tmp_path / "src"})
    source = store.connect(source_settings.storage.db_path)
    _enrol(source)

    target = store.connect(tmp_path / "target.db")
    try:
        counts = db_copy.copy_database(source, target, skip=db_snapshot.SNAPSHOT_EXCLUDE)
    finally:
        source.close()
        target.close()

    assert "web_totp" not in counts
    assert "web_recovery_codes" not in counts
    # web_users is untouched by the exclusion -- only the two 2FA tables are.
    assert counts.get("web_users") == 1


def test_snapshot_exclude_tables_stay_in_copy_tables():
    """The four `web_*` entries `db-migrate` needs must never be removed from
    `copy.TABLES` to "protect" the secret -- that is the wrong mechanism and
    silently un-enrols everyone on a backend switch."""
    for table in db_snapshot.SNAPSHOT_EXCLUDE:
        assert table in db_copy.TABLES
