"""Backend-neutral ``Connection`` / ``Cursor`` wrappers.

The contract these preserve is the one the whole codebase was already written
against — ``sqlite3.Connection``: ``conn.execute(sql, params)`` returns a cursor,
rows index by name and by position, ``cur.lastrowid`` gives the generated key,
and ``conn.commit()`` / ``conn.rollback()`` bound the transaction. Keeping that
contract is what let the backend become pluggable without touching the ~140
call sites that own the SQL.

Per-statement work is a cached dialect translation and a dict lookup, so the
SQLite path stays as cheap as calling the driver directly.

A network database adds a failure mode a file never had: the session can die
between two statements. A batch command holds one connection for hours with
minutes of CPU work between statements, so this wrapper survives that — see
``_recover`` for when replaying a statement is sound and when the transaction is
the caller's to redo.

Deliberately *not* implemented: ``with conn:``. ``sqlite3`` gives that
commit-or-rollback semantics rather than close semantics, and no call site uses
it — a wrapper that quietly picked the other meaning would be a trap.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any, Callable, Iterator, Sequence

from . import errors
from .dialect import Dialect, is_pragma, split_script

log = logging.getLogger("synopticon.db")

Params = Sequence[Any] | None

#: Zero-argument re-acquire, returning ``(connection, release)`` exactly as the
#: constructor takes them. SQLite passes none: a file-backed connection is not
#: something the server hangs up on.
Reopen = Callable[[], "tuple[Any, Callable[[Any], None] | None]"]

#: Pause before each reconnect attempt. `acquire` already blocks for its own pool
#: timeout, so these only have to bridge the tail of a database restart.
_RECONNECT_PAUSES = (0.0, 2.0, 5.0)

#: Statement kinds that leave nothing behind if the socket dies mid-transaction,
#: so replaying one costs nothing. Anything else is assumed to have written.
_READ_ONLY_PREFIXES = ("select", "explain", "pragma", "show")


def _reraise(exc: BaseException) -> None:
    """Re-raise a driver exception as its :mod:`synopticon.db.errors` peer."""
    mapped = errors.translate(exc)
    if mapped is exc:
        raise exc
    raise mapped from exc


def _is_lost(raw: Any) -> bool:
    """True when the driver itself says its connection is past use.

    ``sqlite3`` has neither attribute, so this is a PostgreSQL question in
    practice: psycopg flips both once libpq's socket goes bad.
    """
    return bool(getattr(raw, "closed", False) or getattr(raw, "broken", False))


def _mutates(statement: str) -> bool:
    """Whether ``statement`` may have written anything, judged conservatively."""
    return not statement.lstrip().lower().startswith(_READ_ONLY_PREFIXES)


class Cursor:
    """DB-API cursor with ``sqlite3``'s ergonomics."""

    __slots__ = ("_cur", "_lastrowid")

    def __init__(self, cur: Any, lastrowid: int | None = None) -> None:
        self._cur = cur
        self._lastrowid = lastrowid

    @property
    def lastrowid(self) -> int | None:
        if self._lastrowid is not None:
            return self._lastrowid
        return self._cur.lastrowid

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    @property
    def description(self) -> Any:
        return self._cur.description

    def fetchone(self) -> Any:
        try:
            return self._cur.fetchone()
        except Exception as exc:  # noqa: BLE001 - re-raised as a neutral error
            _reraise(exc)

    def fetchall(self) -> list[Any]:
        try:
            return self._cur.fetchall()
        except Exception as exc:  # noqa: BLE001
            _reraise(exc)
            return []

    def fetchmany(self, size: int | None = None) -> list[Any]:
        try:
            if size is None:
                return self._cur.fetchmany()
            return self._cur.fetchmany(size)
        except Exception as exc:  # noqa: BLE001
            _reraise(exc)
            return []

    def __iter__(self) -> Iterator[Any]:
        return iter(self._cur)

    def close(self) -> None:
        self._cur.close()


class Connection:
    """A live database session.

    ``close()`` is what returns a pooled PostgreSQL connection to its pool, so
    every caller that opens one must close it — the web app's per-request
    factory does so in ``try``/``finally``.
    """

    __slots__ = ("_raw", "_dialect", "_release", "_closed", "_reopen", "_dirty")

    def __init__(
        self,
        raw: Any,
        dialect: Dialect,
        release: Callable[[Any], None] | None = None,
        reopen: Reopen | None = None,
    ) -> None:
        self._raw = raw
        self._dialect = dialect
        self._release = release
        self._reopen = reopen
        self._closed = False
        #: Has this transaction written anything yet? Gates replay after a
        #: reconnect; cleared by every commit and rollback.
        self._dirty = False

    @property
    def dialect(self) -> Dialect:
        return self._dialect

    @property
    def raw(self) -> Any:
        """The underlying driver connection, for backend-specific work."""
        return self._raw

    def execute(self, sql: str, params: Params = None) -> Cursor:
        statement = self._dialect.translate(sql, params is not None)
        returning = self._dialect.returning_clause(statement)
        if returning:
            statement += returning
        cursor = self._guard(lambda: self._run(statement, params, bool(returning)))
        self._dirty = self._dirty or _mutates(statement)
        return cursor

    def executemany(self, sql: str, seq_params: Sequence[Sequence[Any]]) -> Cursor:
        statement = self._dialect.translate(sql, True)

        def work() -> Cursor:
            cur = self._raw.cursor()
            cur.executemany(statement, seq_params)
            return Cursor(cur)

        cursor = self._guard(work)
        self._dirty = self._dirty or _mutates(statement)
        return cursor

    def executescript(self, script: str) -> None:
        """Run a multi-statement script, translating DDL statement by statement."""

        def work() -> None:
            if isinstance(self._raw, sqlite3.Connection):
                self._raw.executescript(script)
                return
            cur = self._raw.cursor()
            for statement in split_script(script):
                if is_pragma(statement):
                    continue
                cur.execute(self._dialect.translate_ddl(statement))

        self._guard(work)
        self._dirty = True

    def commit(self) -> None:
        try:
            self._raw.commit()
        except Exception as exc:  # noqa: BLE001
            _reraise(exc)
        self._dirty = False

    def rollback(self) -> None:
        self._dirty = False
        try:
            self._raw.rollback()
        except Exception as exc:  # noqa: BLE001
            # A session that died took its transaction down with it, so there is
            # nothing left to roll back — and raising here would displace the
            # error the caller is already handling, turning a skipped item into
            # an aborted run.
            if _is_lost(self._raw) or errors.lost_connection(exc):
                return
            _reraise(exc)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._release is None:
            self._raw.close()
            return
        # Returning to the pool: end the transaction ourselves. Unlike sqlite3,
        # psycopg opens one on the first statement of *any* kind, so a read-only
        # request would otherwise hand back a connection still holding a snapshot
        # (and the pool would log every one of them).
        try:
            self._raw.rollback()
        except Exception:  # noqa: BLE001 - a dead connection is the pool's problem
            pass
        self._release(self._raw)

    def _run(self, statement: str, params: Params, returning: bool) -> Cursor:
        cur = self._raw.cursor()
        if params is None:
            cur.execute(statement)
        else:
            cur.execute(statement, params)
        return Cursor(cur, _first_value(cur) if returning else None)

    def _guard(self, work: Callable[[], Any]) -> Any:
        """Run a driver call, translating its errors and replaying it once if the
        session turned out to be dead and replaying is sound."""
        try:
            return work()
        except Exception as exc:  # noqa: BLE001
            if not self._recover(exc):
                _reraise(exc)
                raise
        try:
            return work()
        except Exception as exc:  # noqa: BLE001
            _reraise(exc)
            raise

    def _recover(self, exc: BaseException) -> bool:
        """Reconnect after a lost session, and say whether replay is safe.

        Replaying is only sound while the transaction has written nothing: the
        server rolled the whole transaction back when the socket died, so a retry
        that followed earlier writes would commit a fragment of one. Past that
        point the transaction is the caller's to redo — the batch loops already
        roll back and skip per item, and the statement after that rollback
        reconnects, which is what keeps a multi-hour run alive across a database
        restart.
        """
        if self._closed or self._dirty or self._reopen is None:
            return False
        if not (_is_lost(self._raw) or errors.lost_connection(exc)):
            return False
        return self._reconnect()

    def _reconnect(self) -> bool:
        reopen = self._reopen
        if reopen is None:
            return False
        dead, release = self._raw, self._release
        # Hand the broken connection back before asking for another, or the pool
        # never replaces it — and only once, so `close()` after a failed
        # reconnect cannot release it twice.
        self._release = None
        try:
            if release is not None:
                release(dead)
            else:
                dead.close()
        except Exception:  # noqa: BLE001 - nothing to salvage from a dead socket
            pass
        for pause in _RECONNECT_PAUSES:
            if pause:
                time.sleep(pause)
            try:
                raw, release = reopen()
            except Exception as exc:  # noqa: BLE001 - the last failure is reported by the caller
                log.warning("database reconnect failed: %s", exc)
                continue
            self._raw, self._release, self._dirty = raw, release, False
            log.warning("database connection was lost; reconnected")
            return True
        return False


def _first_value(cur: Any) -> int | None:
    """The single value of a ``RETURNING`` result, or None (``DO NOTHING``)."""
    row = cur.fetchone()
    return None if row is None else int(row[0])
