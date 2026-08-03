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

_pool: Any = None  # SimpleConnectionPool | None
_lock = threading.Lock()


def _import_pool_cls() -> Any:
    """Import SimpleConnectionPool lazily to keep the module import-safe."""
    try:
        from psycopg.pool import SimpleConnectionPool
    except ImportError as e:  # pragma: no cover - depends on env
        raise ConfigError("psycopg/pool not available. Install 'psycopg[binary,pool]'.") from e
    return SimpleConnectionPool


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
        _pool = _import_pool_cls()(
            minconn=2,
            maxconn=10,
            dsn=s.postgres_dsn,
            application_name="uk-regs-pipeline",
        )
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.closeall()
        _pool = None


def init_schema(settings: Settings | None = None) -> None:
    """Run schema.sql against the configured database (idempotent)."""
    import importlib.resources

    pool = get_pool(settings)
    conn = pool.getconn()
    try:
        # schema.sql lives in the same package
        schema = (
            importlib.resources.files("uk_regs_knowledge.relational")
            .joinpath("schema.sql")
            .read_text(encoding="utf-8")
        )
        with conn, conn.cursor() as cur:
            cur.execute(schema)
    finally:
        pool.putconn(conn)
