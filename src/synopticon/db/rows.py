"""A result row that behaves exactly like ``sqlite3.Row``.

The rest of the codebase reads rows four different ways and all four have to
keep working against any backend:

* ``row["cache_key"]`` — by column name (the common case)
* ``row[0]`` — positionally (``setup_routes._count``, ``store._migrate``)
* ``tuple(row)`` — iteration yields *values*, not keys (``lookups.fingerprint``)
* ``dict(row)`` / ``"x" in row.keys()`` — the mapping protocol (``schedules``)

Note the third and fourth pull in opposite directions: a ``collections.abc.
Mapping`` iterates keys, which would silently turn ``fingerprint()`` into a
tuple of column names. ``dict()`` looks for ``keys()`` before falling back to
iteration, so implementing ``keys()`` without inheriting ``Mapping`` satisfies
both — which is precisely the trick ``sqlite3.Row`` itself uses.
"""

from __future__ import annotations

from typing import Any, Iterator


class Row:
    """Immutable result row with name- and index-based access."""

    __slots__ = ("_cols", "_values", "_index")

    def __init__(self, columns: tuple[str, ...], values: tuple[Any, ...]) -> None:
        self._cols = columns
        self._values = tuple(values)
        self._index: dict[str, int] | None = None

    def keys(self) -> list[str]:
        return list(self._cols)

    def __getitem__(self, key: int | str | slice) -> Any:
        if isinstance(key, str):
            if self._index is None:
                self._index = {name: i for i, name in enumerate(self._cols)}
            try:
                return self._values[self._index[key]]
            except KeyError:
                raise IndexError(f"no such column: {key}") from None
        return self._values[key]

    def __iter__(self) -> Iterator[Any]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Row):
            return self._cols == other._cols and self._values == other._values
        if isinstance(other, tuple):
            return self._values == other
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self._cols, self._values))

    def __repr__(self) -> str:
        pairs = ", ".join(f"{k}={v!r}" for k, v in zip(self._cols, self._values))
        return f"<Row {pairs}>"


def row_factory(cursor: Any):
    """psycopg row factory producing :class:`Row` objects."""
    description = cursor.description
    if description is None:
        return lambda values: values
    columns = tuple(d.name for d in description)
    return lambda values: Row(columns, values)
