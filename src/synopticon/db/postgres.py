"""PostgreSQL backend: DSN assembly, connection pooling, driver import guard.

``psycopg`` is an optional dependency behind the ``[postgres]`` extra, so this
module is only ever imported once the config actually selects PostgreSQL — the
CLI's import path stays free of it, the same way ``[review]`` keeps FastAPI out.

Pooling is not a nicety here. ``web/app.py`` opens a connection *per request*,
which against a local SQLite file is free and against a network database is a
TCP round trip plus authentication on every dashboard poll. Unpooled, that
alone would break the responsiveness invariants the web layer is built around.
"""

from __future__ import annotations

import threading
from typing import Any
from urllib.parse import quote

from .rows import row_factory

_INSTALL_HINT = (
    "PostgreSQL support needs the 'postgres' extra: "
    "uv sync --extra postgres  (or: pip install 'synopticon[postgres]')"
)

#: One pool per DSN, process-wide. Job subprocesses build their own.
_pools: dict[str, Any] = {}
_pools_lock = threading.Lock()


def require_driver() -> Any:
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise RuntimeError(_INSTALL_HINT) from exc
    return psycopg


def dsn(config: Any) -> str:
    """Build a libpq connection URI from a ``DatabaseConfig``.

    ``config.url`` is the escape hatch for managed providers that hand out a
    ready-made URI; it wins over the individual fields when set. It is a
    ``SecretStr`` so the web config editor masks it like any other credential.
    """
    url = config.url.get_secret_value().strip()
    if url:
        return url
    password = config.password.get_secret_value()
    auth = quote(config.user, safe="")
    if password:
        auth += ":" + quote(password, safe="")
    uri = f"postgresql://{auth}@{config.host}:{config.port}/{quote(config.database, safe='')}"
    if config.sslmode:
        uri += f"?sslmode={config.sslmode}"
    return uri


def _make_pool(conninfo: str, size: int) -> Any:
    from psycopg_pool import ConnectionPool

    pool = ConnectionPool(
        conninfo,
        min_size=1,
        max_size=max(1, size),
        kwargs={"row_factory": row_factory},
        # Hand out a connection only after checking it is still alive: a database
        # restart or an idle-timeout reaper would otherwise surface as a failed
        # request rather than a transparent reconnect.
        check=ConnectionPool.check_connection,
        open=True,
        name="synopticon",
    )
    pool.wait(timeout=30)
    return pool


def acquire(conninfo: str, size: int) -> tuple[Any, Any]:
    """Check out a raw connection; returns ``(connection, release_callable)``."""
    require_driver()
    try:
        pool = _pool(conninfo, size)
    except ImportError:
        # psycopg_pool ships with the extra, but a hand-assembled environment may
        # have only psycopg. Unpooled still works; it is just slower per request.
        import psycopg

        conn = psycopg.connect(conninfo, row_factory=row_factory)
        return conn, lambda c: c.close()
    return pool.getconn(timeout=30), pool.putconn


def _pool(conninfo: str, size: int) -> Any:
    with _pools_lock:
        pool = _pools.get(conninfo)
        if pool is None:
            pool = _make_pool(conninfo, size)
            _pools[conninfo] = pool
        return pool


def close_pools() -> None:
    """Close every pool this process opened (test teardown, app shutdown)."""
    with _pools_lock:
        for pool in _pools.values():
            pool.close()
        _pools.clear()


#: Arbitrary but fixed key for the migration advisory lock. Two processes
#: starting at once (web server plus a job subprocess) must not both try to
#: apply the same migration.
MIGRATION_LOCK_KEY = 0x53594E4F  # 'SYNO'
