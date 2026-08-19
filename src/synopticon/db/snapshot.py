"""Point-in-time snapshot of the whole library into a single SQLite file.

The GUI's "Download database" button needs one artifact it can hand to a
browser, whichever backend is configured, and one that is useful on the way
back in. SQLite already is that artifact, so its snapshot is a ``VACUUM INTO``:
one statement, consistent under a reader transaction, compacted on the way out,
and safe to take while a job is writing. PostgreSQL has no such file, so its
snapshot is built by replaying :mod:`synopticon.db.copy` into a fresh SQLite
database — the same table-ordered copy the backend switch uses, run in the
other direction. Either way the download restores by being dropped in as
``data/synopticon.db``, or copied back into PostgreSQL with ``db-migrate --from``.

A PostgreSQL snapshot is *not* transactionally consistent across tables: the
copy is a sequence of reads, not one repeatable-read transaction. That is the
same guarantee ``db-migrate`` gives, and the same caveat applies — take it while
the library is quiescent if the exact instant matters.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Settings

log = logging.getLogger("synopticon.db")

__all__ = ["snapshot", "source_bytes"]


def snapshot(settings: "Settings", dest: Path) -> Path:
    """Write a self-contained SQLite copy of the configured database to ``dest``.

    ``dest`` must not exist yet (``VACUUM INTO`` refuses to overwrite, and the
    PostgreSQL path wants an empty database to copy into). Returns ``dest``.
    """
    dest = Path(dest)
    if dest.exists():
        raise FileExistsError(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if settings.database.backend == "postgres":
        _snapshot_postgres(settings, dest)
    else:
        _snapshot_sqlite(Path(settings.storage.db_path), dest)
    log.info("wrote database snapshot to %s", dest)
    return dest


def source_bytes(settings: "Settings") -> int | None:
    """Size of the live SQLite database, or ``None`` when there is no file.

    Advisory only — it is what the Utilities card shows next to the download
    button so the size is not a surprise. PostgreSQL has no answer that is both
    cheap and honest (the snapshot is a re-encoding, not a copy of the server's
    on-disk layout), so it gets ``None``.
    """
    if settings.database.backend == "postgres":
        return None
    try:
        return Path(settings.storage.db_path).stat().st_size
    except OSError:
        return None


def _snapshot_sqlite(db_path: Path, dest: Path) -> None:
    if not db_path.exists():
        raise FileNotFoundError(db_path)
    raw = sqlite3.connect(db_path, timeout=60)
    try:
        raw.execute("VACUUM INTO ?", (str(dest),))
    finally:
        raw.close()


def _snapshot_postgres(settings: "Settings", dest: Path) -> None:
    from . import copy as db_copy
    from . import store

    source = store.connect(settings)
    try:
        # `store.connect` on a path migrates the fresh file to the current
        # schema, which is exactly what `copy_database` expects of its target.
        target = store.connect(dest)
        try:
            db_copy.copy_database(source, target)
        finally:
            target.close()
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    finally:
        source.close()
