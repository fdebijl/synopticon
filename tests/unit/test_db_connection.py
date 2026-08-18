"""The connection wrapper's behaviour when a network database drops the session.

A batch command holds one connection for hours with minutes of CPU work between
statements, so PostgreSQL restarting — or a firewall forgetting an idle socket —
must not end the run. These use a stub driver rather than a real server: what is
under test is the wrapper's replay-safety rule, not libpq.
"""

from __future__ import annotations

import importlib.util
import sqlite3

import pytest

from synopticon.db import errors
from synopticon.db.connection import Connection
from synopticon.db.dialect import Dialect

DIALECT = Dialect()

LOST = "consuming input failed: server closed the connection unexpectedly"


class FakeCursor:
    def __init__(self, conn: "FakeConn") -> None:
        self._conn = conn

    def execute(self, statement, params=None):
        self._conn.guard()
        self._conn.statements.append(statement)

    def executemany(self, statement, seq_params):
        self._conn.guard()
        self._conn.statements.append(statement)

    def fetchone(self):
        return (1,)


class FakeConn:
    """Just enough of a psycopg connection: it can go bad, and says so."""

    def __init__(self) -> None:
        self.closed = False
        self.broken = False
        self.statements: list[str] = []
        self.commits = 0
        self.rollbacks = 0
        self.rollback_error: BaseException | None = None

    def guard(self) -> None:
        if self.closed:
            raise sqlite3.OperationalError(LOST)

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.guard()
        self.commits += 1

    def rollback(self) -> None:
        if self.rollback_error is not None:
            raise self.rollback_error
        self.guard()
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True

    def kill(self) -> None:
        """What a server hanging up looks like from the driver's side."""
        self.closed = True
        self.broken = True


class Pool:
    """Hands out a fresh FakeConn per acquire, recording what came back."""

    def __init__(self) -> None:
        self.handed: list[FakeConn] = []
        self.released: list[FakeConn] = []
        self.fail = 0

    def acquire(self):
        if self.fail:
            self.fail -= 1
            raise sqlite3.OperationalError("connection is bad")
        conn = FakeConn()
        self.handed.append(conn)
        return conn, self.released.append


@pytest.fixture
def pooled():
    pool = Pool()
    raw, release = pool.acquire()
    return Connection(raw, DIALECT, release, reopen=pool.acquire), pool


class TestLostConnection:
    def test_a_dead_session_is_replaced_and_the_statement_replayed(self, pooled):
        conn, pool = pooled
        conn.execute("SELECT 1")
        pool.handed[0].kill()

        conn.execute("SELECT 2")

        assert len(pool.handed) == 2
        assert pool.handed[1].statements == ["SELECT 2"]
        # The broken connection went back to the pool, or the pool never replaces it.
        assert pool.released == [pool.handed[0]]

    def test_the_first_write_of_a_transaction_is_replayed(self, pooled):
        conn, pool = pooled
        pool.handed[0].kill()

        conn.execute("DELETE FROM faces WHERE space = ?", ("personal",))

        assert pool.handed[1].statements == ["DELETE FROM faces WHERE space = ?"]

    def test_a_write_that_follows_another_write_is_never_replayed(self, pooled):
        """Replaying it would commit a fragment of a transaction the server
        already rolled back — the caller has to redo the whole thing."""
        conn, pool = pooled
        conn.execute("DELETE FROM faces WHERE photo_id = ?", (1,))
        pool.handed[0].kill()

        with pytest.raises(errors.OperationalError):
            conn.execute("INSERT INTO faces (space) VALUES (?)", ("personal",))

        assert len(pool.handed) == 1

    def test_a_read_is_replayed_even_after_a_write(self, pooled):
        conn, pool = pooled
        conn.execute("SELECT 1")
        pool.handed[0].kill()
        conn.execute("SELECT 2")
        assert len(pool.handed) == 2

    def test_rollback_then_the_next_statement_recovers(self, pooled):
        """The extract loop's exact shape: one photo per transaction, and a
        failure rolls back and skips instead of ending the run."""
        conn, pool = pooled
        conn.execute("DELETE FROM faces WHERE photo_id = ?", (1,))
        pool.handed[0].kill()
        with pytest.raises(errors.OperationalError):
            conn.execute("INSERT INTO faces (space) VALUES (?)", ("personal",))

        conn.rollback()  # must not raise, or the skip becomes an aborted run
        conn.execute("DELETE FROM faces WHERE photo_id = ?", (2,))
        conn.commit()

        assert pool.handed[1].commits == 1

    def test_executemany_recovers_at_a_transaction_boundary(self, pooled):
        conn, pool = pooled
        pool.handed[0].kill()
        conn.executemany("INSERT INTO photos (id) VALUES (?)", [(1,), (2,)])
        assert pool.handed[1].statements == ["INSERT INTO photos (id) VALUES (?)"]

    def test_reconnecting_gives_up_and_reports_the_original_failure(self, pooled, monkeypatch):
        monkeypatch.setattr("synopticon.db.connection._RECONNECT_PAUSES", (0.0, 0.0))
        conn, pool = pooled
        pool.handed[0].kill()
        pool.fail = 99

        with pytest.raises(errors.OperationalError, match="server closed the connection"):
            conn.execute("SELECT 1")

    def test_a_sqlstate_of_the_connection_class_counts_as_lost(self):
        exc = sqlite3.OperationalError("terminating connection due to administrator command")
        exc.sqlstate = "57P01"  # type: ignore[attr-defined]
        assert errors.lost_connection(exc)

    def test_an_ordinary_statement_error_is_not_a_lost_connection(self):
        exc = sqlite3.OperationalError('relation "faces" does not exist')
        exc.sqlstate = "42P01"  # type: ignore[attr-defined]
        assert not errors.lost_connection(exc)


class TestRollback:
    def test_a_rollback_on_a_dead_session_is_silent(self, pooled):
        conn, pool = pooled
        pool.handed[0].kill()
        conn.rollback()  # nothing left to roll back: the server already did

    def test_a_rollback_that_fails_for_another_reason_still_raises(self, pooled):
        conn, pool = pooled
        pool.handed[0].rollback_error = sqlite3.OperationalError("disk I/O error")
        with pytest.raises(errors.OperationalError, match="disk I/O error"):
            conn.rollback()

    def test_a_failed_commit_is_never_swallowed(self, pooled):
        conn, pool = pooled
        pool.handed[0].kill()
        with pytest.raises(errors.OperationalError):
            conn.commit()


@pytest.mark.skipif(
    importlib.util.find_spec("psycopg") is None,
    reason="psycopg not installed (needs the [postgres] extra)",
)
class TestKeepalives:
    def test_defaults_are_layered_under_the_dsn(self):
        from synopticon.db import postgres

        kwargs = postgres.connect_kwargs("postgresql://user@db:5432/synopticon")
        assert kwargs["keepalives"] == "1"
        assert int(kwargs["keepalives_idle"]) <= 60

    def test_an_explicit_setting_in_the_dsn_wins(self):
        from synopticon.db import postgres

        kwargs = postgres.connect_kwargs(
            "postgresql://user@db:5432/synopticon?keepalives_idle=600"
        )
        assert "keepalives_idle" not in kwargs
        assert kwargs["keepalives"] == "1"


class TestSqliteIsUnaffected:
    def test_an_error_surfaces_instead_of_reconnecting(self, tmp_path):
        from synopticon.db import store

        conn = store.connect(tmp_path / "test.db")
        try:
            with pytest.raises(errors.OperationalError):
                conn.execute("SELECT * FROM nope")
            conn.rollback()
            assert conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 0
        finally:
            conn.close()
