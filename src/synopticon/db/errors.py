"""Driver-neutral database exceptions.

Every DB-API driver raises its own exception classes, so code that catches
``sqlite3.IntegrityError`` breaks the moment the backend is PostgreSQL. The
connection wrapper in :mod:`synopticon.db.connection` translates whatever the
driver raised into one of these, and callers catch these instead.
"""

from __future__ import annotations

import sqlite3
import sys


class DatabaseError(Exception):
    """Base for every database failure, whatever the backend."""


class IntegrityError(DatabaseError):
    """A constraint was violated (unique, foreign key, not-null)."""


class OperationalError(DatabaseError):
    """The statement could not run: missing table, lost connection, timeout."""


def _driver_bases() -> list[tuple[type, type, type]]:
    """(base, integrity, operational) triples for every importable driver.

    psycopg's concrete errors are subclasses (``UniqueViolation``,
    ``UndefinedTable``, …), so this matches by isinstance against the DB-API
    base classes rather than by exception name.
    """
    triples = [(sqlite3.Error, sqlite3.IntegrityError, sqlite3.OperationalError)]
    psycopg = sys.modules.get("psycopg")
    if psycopg is not None:
        triples.append(
            (psycopg.Error, psycopg.IntegrityError, psycopg.OperationalError)
        )
    return triples


#: SQLSTATEs that mean the *session* is gone rather than the statement rejected:
#: class 08 (connection exception) plus the operator and crash shutdowns. A socket
#: that died before the server could answer carries no SQLSTATE at all, which is
#: why :mod:`synopticon.db.connection` asks the connection's own state as well.
_LOST_CONNECTION_STATES = frozenset(
    {"08000", "08001", "08003", "08004", "08006", "08P01", "57P01", "57P02", "57P03"}
)


def lost_connection(exc: BaseException) -> bool:
    """True when ``exc`` reports that the connection itself no longer exists."""
    return getattr(exc, "sqlstate", None) in _LOST_CONNECTION_STATES


def translate(exc: BaseException) -> BaseException:
    """Map a driver exception onto the neutral hierarchy.

    Anything that is not a DB-API error is returned unchanged — this only ever
    narrows driver exceptions, it never swallows a bug.
    """
    for base, integrity, operational in _driver_bases():
        if not isinstance(exc, base):
            continue
        if isinstance(exc, integrity):
            return IntegrityError(str(exc))
        if isinstance(exc, operational):
            return OperationalError(str(exc))
        return DatabaseError(str(exc))
    return exc
