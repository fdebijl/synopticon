"""Database layer: one schema, one set of SQL, pluggable backend.

Import the neutral types from here rather than reaching for ``sqlite3``::

    from synopticon.db import Connection, Row, errors

``synopticon.db.store`` holds the connection factory and the shared helpers.
"""

from __future__ import annotations

from . import errors
from .connection import Connection, Cursor
from .rows import Row

__all__ = ["Connection", "Cursor", "Row", "errors"]
