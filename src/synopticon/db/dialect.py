"""SQL dialect translation.

Modules own their SQL and write it in SQLite's dialect — that stays the source
of truth. This module is the only place that knows how to say the same thing to
another backend, so adding a backend never means a second copy of a query or of
``schema.sql``.

Two translation surfaces, deliberately separate:

* :meth:`Dialect.translate` — runtime DML/DQL. Only placeholder style differs,
  because no query in the codebase uses a SQLite-only function.
* :meth:`Dialect.translate_ddl` — migration scripts, where column types and
  autoincrement syntax diverge.

Everything is driven by a character scanner rather than bare regexes: a ``?``
inside a string literal is data, not a placeholder, and ``--`` starts a comment
that may itself contain either.
"""

from __future__ import annotations

import re
from functools import lru_cache

#: Tables whose primary key is an identity/autoincrement column, discovered
#: from the migration DDL by :func:`scan_identity_columns` rather than hardcoded,
#: so a new table with an autoincrement id needs no change here.
IdentityMap = dict[str, str]


def _chunks(sql: str) -> list[tuple[str, bool]]:
    """Split ``sql`` into (text, is_code) chunks.

    ``is_code`` is False for string literals, quoted identifiers and comments —
    the regions where a ``?`` or ``;`` is content rather than syntax.
    """
    out: list[tuple[str, bool]] = []
    buf: list[str] = []
    i, n = 0, len(sql)

    def flush(is_code: bool) -> None:
        if buf:
            out.append(("".join(buf), is_code))
            buf.clear()

    while i < n:
        ch = sql[i]
        if ch in "'\"":
            flush(True)
            quote = ch
            j = i + 1
            while j < n:
                if sql[j] == quote:
                    if j + 1 < n and sql[j + 1] == quote:  # doubled = escaped
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            out.append((sql[i:j], False))
            i = j
        elif sql.startswith("--", i):
            flush(True)
            j = sql.find("\n", i)
            j = n if j == -1 else j
            out.append((sql[i:j], False))
            i = j
        elif sql.startswith("/*", i):
            flush(True)
            j = sql.find("*/", i + 2)
            j = n if j == -1 else j + 2
            out.append((sql[i:j], False))
            i = j
        else:
            buf.append(ch)
            i += 1
    flush(True)
    return out


def split_script(sql: str) -> list[str]:
    """Split a multi-statement script into individual statements.

    ``sqlite3.executescript`` does this for us; every other driver wants one
    statement per call.
    """
    statements: list[str] = []
    current: list[str] = []
    for text, is_code in _chunks(sql):
        if not is_code:
            current.append(text)
            continue
        start = 0
        for m in re.finditer(";", text):
            current.append(text[start : m.start()])
            statements.append("".join(current))
            current = []
            start = m.end()
        current.append(text[start:])
    statements.append("".join(current))
    return [s for s in (st.strip() for st in statements) if _has_code(s)]


def _has_code(statement: str) -> bool:
    return any(is_code and text.strip() for text, is_code in _chunks(statement))


def scan_identity_columns(sql_texts: list[str]) -> IdentityMap:
    """Map ``table -> column`` for every ``INTEGER PRIMARY KEY AUTOINCREMENT``.

    PostgreSQL has no ``lastrowid``; the value has to come back via ``RETURNING``,
    which needs the column name. Reading it out of the DDL keeps that knowledge
    in one place — the schema — instead of a hand-maintained list that silently
    goes stale when a migration adds a table.
    """
    table_re = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w.]+)", re.I)
    col_re = re.compile(r"^\s*([\w]+)\s+INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT", re.I)
    found: IdentityMap = {}
    for sql in sql_texts:
        table = ""
        for line in sql.splitlines():
            m = table_re.search(line)
            if m:
                table = m.group(1).lower()
            m = col_re.match(line)
            if m and table:
                found[table] = m.group(1)
    return found


class Dialect:
    """Base dialect: SQLite's own, i.e. no translation at all."""

    name = "sqlite"
    #: True when the driver returns the generated key via ``cursor.lastrowid``.
    has_lastrowid = True

    def translate(self, sql: str, has_params: bool) -> str:
        return sql

    def translate_ddl(self, sql: str) -> str:
        return sql

    def insert_target(self, sql: str) -> str | None:
        """The table an ``INSERT`` writes to, or None if ``sql`` is not one."""
        m = re.match(r"\s*INSERT\s+(?:OR\s+\w+\s+)?INTO\s+([\w.]+)", sql, re.I)
        return m.group(1).lower() if m else None

    def returning_clause(self, sql: str) -> str | None:
        """``RETURNING <pk>`` to append so ``lastrowid`` has something to read.

        None on backends that report the generated key themselves.
        """
        return None


class PostgresDialect(Dialect):
    """PostgreSQL.

    Shares SQLite's ``ON CONFLICT (...) DO UPDATE SET x = excluded.x`` upsert
    syntax verbatim, which is why every upsert in the codebase needs no rewrite
    at all. What does differ: ``%s`` placeholders, ``BYTEA`` for blobs, identity
    columns instead of ``AUTOINCREMENT``, and ``jsonb`` operators instead of
    ``json_extract``.
    """

    name = "postgres"
    has_lastrowid = False

    def __init__(self, identity_columns: IdentityMap | None = None) -> None:
        self.identity_columns: IdentityMap = identity_columns or {}

    def translate(self, sql: str, has_params: bool) -> str:
        return _translate_pg(sql, has_params)

    def translate_ddl(self, sql: str) -> str:
        return _translate_pg_ddl(sql)

    def returning_clause(self, sql: str) -> str | None:
        """``RETURNING <pk>`` to append so ``lastrowid`` has something to read."""
        table = self.insert_target(sql)
        if table is None or re.search(r"\bRETURNING\b", sql, re.I):
            return None
        column = self.identity_columns.get(table)
        return f" RETURNING {column}" if column else None


@lru_cache(maxsize=1024)
def _translate_pg(sql: str, has_params: bool) -> str:
    out: list[str] = []
    for text, is_code in _chunks(sql):
        if has_params:
            # A literal '%' has to be doubled or psycopg reads it as the start of
            # a placeholder. Only when binding, though: psycopg skips placeholder
            # parsing entirely for a parameterless statement, so escaping there
            # would leave '%%' in the SQL.
            text = text.replace("%", "%%")
        if is_code:
            text = text.replace("?", "%s")
        out.append(text)
    return "".join(out)


_JSON_EXTRACT = re.compile(
    r"json_extract\(\s*([\w.]+)\s*,\s*'\$\.([^']+)'\s*\)", re.I
)


@lru_cache(maxsize=256)
def _translate_pg_ddl(sql: str) -> str:
    # Comments go first. They are prose, so they are the one place a word like
    # "REAL" can appear meaning something other than a column type — and no
    # backend needs them.
    stripped = "".join(
        text
        for text, is_code in _chunks(sql)
        if is_code or not (text.startswith("--") or text.startswith("/*"))
    )
    # json_extract's second argument is a string literal, so this rewrite has to
    # see the statement whole rather than code-only chunks.
    stripped = _JSON_EXTRACT.sub(_json_extract_pg, stripped)

    out: list[str] = []
    for text, is_code in _chunks(stripped):
        if not is_code:
            out.append(text)
            continue
        text = re.sub(
            r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b",
            "BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY",
            text,
            flags=re.I,
        )
        text = re.sub(r"\bBLOB\b", "BYTEA", text, flags=re.I)
        # SQLite REAL is a 64-bit float; PostgreSQL REAL is 32-bit. Face bboxes
        # take part in a UNIQUE key, so narrowing them would collapse distinct
        # detections into a constraint violation.
        text = re.sub(r"\bREAL\b", "DOUBLE PRECISION", text, flags=re.I)
        out.append(text)
    return "".join(out).strip()


def _json_extract_pg(m: re.Match[str]) -> str:
    column, path = m.group(1), m.group(2)
    return f"({column}::jsonb #>> '{{{','.join(path.split('.'))}}}')"


def is_pragma(sql: str) -> bool:
    """True when the statement is a ``PRAGMA``, which only SQLite understands."""
    return bool(re.match(r"\s*PRAGMA\b", sql, re.I))
