"""
psycopg 3 connection pool singleton.

Lazy-initialised on first use. Use ``get_pool()`` everywhere; ``close_pool()``
on shutdown.

The ``psycopg`` import is deferred to call-time so that this package imports
cleanly in "dev/offline" mode (no DB required) — the rest of the pipeline can
run with deterministic extraction + reconciliation without a Postgres backend.
"""

from __future__ import annotations

import threading
from typing import Any

from uk_regs_core import ConfigError, Settings, get_settings

_pool: Any = None  # psycopg_pool.ConnectionPool | None
_lock = threading.Lock()


def _import_pool_cls() -> Any:
    """Import ConnectionPool lazily to keep the module import-safe."""
    try:
        from psycopg_pool import ConnectionPool
    except ImportError as e:  # pragma: no cover - depends on env
        raise ConfigError("psycopg-pool not available. Install 'psycopg[binary,pool]'.") from e
    return ConnectionPool


def get_pool(settings: Settings | None = None) -> Any:
    global _pool
    if _pool is not None:
        return _pool
    with _lock:
        if _pool is not None:
            return _pool
        s = settings or get_settings()
        if not s.postgres_dsn:
            raise ConfigError("POSTGRES_DSN is not set")
        # psycopg 3 API: ConnectionPool(conninfo=..., min_size=..., max_size=...).
        # ``application_name`` and other libpq params are passed via ``kwargs``.
        _pool = _import_pool_cls()(
            conninfo=s.postgres_dsn,
            min_size=2,
            max_size=10,
            kwargs={"application_name": "uk-regs-pipeline"},
        )
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def init_schema(settings: Settings | None = None) -> None:
    """Run schema.sql against the configured database (idempotent)."""
    import importlib.resources

    pool = get_pool(settings)
    # schema.sql lives in the same package
    schema = (
        importlib.resources.files("uk_regs_knowledge.relational")
        .joinpath("schema.sql")
        .read_text(encoding="utf-8")
    )
    # ``pool.connection()`` checks out a connection, commits on a clean exit,
    # rolls back on error, and returns the connection to the pool automatically.
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(schema)
