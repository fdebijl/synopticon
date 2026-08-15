"""Backend-neutral ``Connection`` / ``Cursor`` wrappers.

The contract these preserve is the one the whole codebase was already written
against — ``sqlite3.Connection``: ``conn.execute(sql, params)`` returns a cursor,
rows index by name and by position, ``cur.lastrowid`` gives the generated key,
and ``conn.commit()`` / ``conn.rollback()`` bound the transaction. Keeping that
contract is what let the backend become pluggable without touching the ~140
call sites that own the SQL.

Per-statement work is a cached dialect translation and a dict lookup, so the
SQLite path stays as cheap as calling the driver directly.

Deliberately *not* implemented: ``with conn:``. ``sqlite3`` gives that
commit-or-rollback semantics rather than close semantics, and no call site uses
it — a wrapper that quietly picked the other meaning would be a trap.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Callable, Iterator, Sequence

from . import errors
from .dialect import Dialect, is_pragma, split_script

Params = Sequence[Any] | None


def _reraise(exc: BaseException) -> None:
    """Re-raise a driver exception as its :mod:`synopticon.db.errors` peer."""
    mapped = errors.translate(exc)
    if mapped is exc:
        raise exc
    raise mapped from exc


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

    __slots__ = ("_raw", "_dialect", "_release", "_closed")

    def __init__(
        self,
        raw: Any,
        dialect: Dialect,
        release: Callable[[Any], None] | None = None,
    ) -> None:
        self._raw = raw
        self._dialect = dialect
        self._release = release
        self._closed = False

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
        try:
            cur = self._raw.cursor()
            if params is None:
                cur.execute(statement)
            else:
                cur.execute(statement, params)
            lastrowid = _first_value(cur) if returning else None
        except Exception as exc:  # noqa: BLE001
            _reraise(exc)
            raise
        return Cursor(cur, lastrowid)

    def executemany(self, sql: str, seq_params: Sequence[Sequence[Any]]) -> Cursor:
        statement = self._dialect.translate(sql, True)
        try:
            cur = self._raw.cursor()
            cur.executemany(statement, seq_params)
        except Exception as exc:  # noqa: BLE001
            _reraise(exc)
            raise
        return Cursor(cur)

    def executescript(self, script: str) -> None:
        """Run a multi-statement script, translating DDL statement by statement."""
        try:
            if isinstance(self._raw, sqlite3.Connection):
                self._raw.executescript(script)
                return
            cur = self._raw.cursor()
            for statement in split_script(script):
                if is_pragma(statement):
                    continue
                cur.execute(self._dialect.translate_ddl(statement))
        except Exception as exc:  # noqa: BLE001
            _reraise(exc)

    def commit(self) -> None:
        try:
            self._raw.commit()
        except Exception as exc:  # noqa: BLE001
            _reraise(exc)

    def rollback(self) -> None:
        try:
            self._raw.rollback()
        except Exception as exc:  # noqa: BLE001
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


def _first_value(cur: Any) -> int | None:
    """The single value of a ``RETURNING`` result, or None (``DO NOTHING``)."""
    row = cur.fetchone()
    return None if row is None else int(row[0])
