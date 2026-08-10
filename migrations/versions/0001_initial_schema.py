"""initial schema baseline

Revision ID: 0001_initial
Revises:
Create Date: 2025-01-01 00:00:00

Establishes the full UK Regs Pipeline schema (documents, rules, figures,
change_sets, supersessions, audit_events, source_documents, source_poll_state)
including pgvector and full-text-search support.

This migration executes the canonical ``schema.sql`` that is shipped inside the
``uk_regs_knowledge`` package, guaranteeing that ``alembic upgrade head`` on a
fresh database produces a schema identical to ``make init-db``.

For databases that were already initialised via ``init_schema()`` (the legacy
idempotent-SQL path), this baseline is marked as applied by running
``alembic stamp head`` instead of ``upgrade``.
"""

from __future__ import annotations

import importlib.resources
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _schema_sql() -> str:
    """Read the canonical schema.sql from the uk_regs_knowledge package."""
    return (
        importlib.resources.files("uk_regs_knowledge.relational")
        .joinpath("schema.sql")
        .read_text(encoding="utf-8")
    )


def upgrade() -> None:
    op.execute(_schema_sql())


def downgrade() -> None:
    """Drop all tables and extensions created by the baseline."""
    op.execute("DROP TABLE IF EXISTS source_poll_state CASCADE")
    op.execute("DROP TABLE IF EXISTS source_documents CASCADE")
    op.execute("DROP TABLE IF EXISTS audit_events CASCADE")
    op.execute("DROP TABLE IF EXISTS supersessions CASCADE")
    op.execute("DROP TABLE IF EXISTS rule_deltas CASCADE")
    op.execute("DROP TABLE IF EXISTS change_sets CASCADE")
    op.execute("DROP TABLE IF EXISTS figures CASCADE")
    op.execute("DROP TABLE IF EXISTS rules CASCADE")
    op.execute("DROP TABLE IF EXISTS document_versions CASCADE")
    op.execute("DROP TABLE IF EXISTS documents CASCADE")
    # NOTE: extensions left in place — other databases on the cluster may use them.
