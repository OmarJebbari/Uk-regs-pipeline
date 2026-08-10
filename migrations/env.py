"""
Alembic migration environment.

The database URL is resolved at runtime from ``POSTGRES_DSN`` (via
``uk_regs_core.Settings``) rather than from ``alembic.ini``, so secrets never
live in VCS. If ``POSTGRES_DSN`` is unset we fall back to the value in
``alembic.ini`` (empty by default) — this lets ``alembic`` commands print help
without raising.

This project uses raw SQL migrations (executed via ``op.execute``) rather than
SQLAlchemy ``op.create_table`` directives, because the schema relies heavily on
Postgres-specific features (pgvector, generated tsvector columns, GIN/ivfflat
indexes) that are awkward to express through the SQLAlchemy metadata API.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _resolve_url() -> str:
    """Resolve the target database URL from the environment."""
    # Priority: explicit env var → Settings.postgres_dsn → alembic.ini
    dsn = os.environ.get("POSTGRES_DSN", "").strip()
    if not dsn:
        try:
            from uk_regs_core import get_settings

            dsn = get_settings().postgres_dsn or ""
        except Exception:
            dsn = ""
    if not dsn:
        dsn = config.get_main_option("sqlalchemy.url") or ""
    return dsn


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL to stdout)."""
    url = _resolve_url()
    context.configure(
        url=url,
        target_metadata=None,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (connect to the DB and apply)."""
    url = _resolve_url()
    if not url:
        raise RuntimeError(
            "No database URL configured. Set POSTGRES_DSN or sqlalchemy.url in alembic.ini."
        )

    # Inject the resolved URL into the section Alembic reads.
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = url

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
