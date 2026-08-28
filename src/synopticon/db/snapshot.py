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

__all__ = ["snapshot", "source_bytes", "SNAPSHOT_EXCLUDE"]

#: Never copied into a snapshot. `web_totp.secret` is plaintext base32: a
#: snapshot carrying it hands the holder a working second factor forever, and
#: unlike a password hash there is nothing to crack. Recovery codes are the same
#: credential by another name, and a live login challenge is a session in
#: waiting. These are NOT removed from `copy.TABLES`: `db-migrate` and the
#: backend switch must keep them, or moving to PostgreSQL silently un-enrols
#: everyone. `web_auth_log` is deliberately NOT excluded -- it is evidence, it
#: contains no credential, and an operator restoring a backup wants it. Nor is
#: `web_sessions`: its new pin_hash is a sha256 of facts the holder of a backup
#: can already observe, not a credential, and a restore is expected to bring
#: sessions back.
SNAPSHOT_EXCLUDE: frozenset[str] = frozenset(
    {"web_totp", "web_recovery_codes", "web_login_challenges"}
)


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

    # `VACUUM INTO` is a whole-file copy, so the excluded tables ride along in
    # `dest` -- strip them from the copy, not the source. A table can be absent
    # when `db_path` predates migration 10.
    out = sqlite3.connect(dest, timeout=60)
    try:
        existing = {
            row[0]
            for row in out.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        for table in SNAPSHOT_EXCLUDE:
            if table in existing:
                out.execute(f"DELETE FROM {table}")
        out.commit()
        out.execute("VACUUM")
    finally:
        out.close()


def _snapshot_postgres(settings: "Settings", dest: Path) -> None:
    from . import copy as db_copy
    from . import store

    source = store.connect(settings)
    try:
        # `store.connect` on a path migrates the fresh file to the current
        # schema, which is exactly what `copy_database` expects of its target.
        target = store.connect(dest)
        try:
            db_copy.copy_database(source, target, skip=SNAPSHOT_EXCLUDE)
        finally:
            target.close()
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    finally:
        source.close()
