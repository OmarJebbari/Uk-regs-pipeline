"""
State store — tracks which documents we have already seen/ingested per source.

Two implementations:
    * InMemoryStateStore — for tests and offline dev (no DB needed)
    * StateStore         — Postgres-backed (default in prod)

Both share the same Protocol so the monitor is agnostic to the backend.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Protocol

from uk_regs_sourcing.models import SourceState

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
#  Protocol
# --------------------------------------------------------------------------- #
class StateStore(Protocol):
    """Tracks seen documents to enable diffing."""

    def get_seen(self, source_id: str) -> dict[str, SourceState]:
        """Return {document_id: state} for documents already ingested."""

    def mark_seen(self, source_id: str, state: SourceState) -> None:
        """Record a document as ingested (upsert)."""

    def last_poll(self, source_id: str) -> datetime | None:
        """When did we last successfully poll this source?"""

    def set_last_poll(self, source_id: str, when: datetime) -> None:
        """Record a successful poll timestamp."""


# --------------------------------------------------------------------------- #
#  In-memory (tests + offline dev)
# --------------------------------------------------------------------------- #
class InMemoryStateStore:
    """Process-local state store. Resets on restart."""

    def __init__(self) -> None:
        self._seen: dict[str, dict[str, SourceState]] = {}
        self._polls: dict[str, datetime] = {}

    def get_seen(self, source_id: str) -> dict[str, SourceState]:
        return dict(self._seen.get(source_id, {}))

    def mark_seen(self, source_id: str, state: SourceState) -> None:
        self._seen.setdefault(source_id, {})[state.document_id] = state

    def last_poll(self, source_id: str) -> datetime | None:
        return self._polls.get(source_id)

    def set_last_poll(self, source_id: str, when: datetime) -> None:
        self._polls[source_id] = when


# --------------------------------------------------------------------------- #
#  Postgres-backed
# --------------------------------------------------------------------------- #
class PostgresStateStore:
    """
    Stores seen documents in the ``source_documents`` table.

    Schema (created by schema.sql):
        source_id        TEXT
        document_id      TEXT
        version          TEXT
        title            TEXT
        url              TEXT
        sha256           TEXT
        size_bytes       BIGINT
        published_at     TIMESTAMPTZ
        first_seen_at    TIMESTAMPTZ DEFAULT now()
        last_checked_at  TIMESTAMPTZ DEFAULT now()
        status           TEXT DEFAULT 'ingested'
        PRIMARY KEY (source_id, document_id)
    """

    def __init__(self) -> None:
        # Lazy: the pool is only touched at call-time so import stays cheap.
        from uk_regs_knowledge.relational.connection import get_pool

        self._pool_getter = get_pool

    @contextmanager
    def _conn(self) -> Iterator:
        """Borrow a pooled connection (psycopg 3 ``pool.connection()``).

        Checks out a connection, commits on a clean exit, rolls back on error,
        and returns the connection to the pool automatically.
        """
        with self._pool_getter().connection() as conn:
            yield conn

    def get_seen(self, source_id: str) -> dict[str, SourceState]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT document_id, version, title, url, sha256, size_bytes, published_at "
                "FROM source_documents WHERE source_id = %s",
                (source_id,),
            )
            rows = cur.fetchall()
        out: dict[str, SourceState] = {}
        for (
            document_id,
            version,
            title,
            url,
            sha256,
            size_bytes,
            published_at,
        ) in rows:
            out[document_id] = SourceState(
                source_id=source_id,
                document_id=document_id,
                version=version,
                title=title,
                url=url,
                sha256=sha256,
                size_bytes=size_bytes,
                published_at=published_at,
            )
        return out

    def mark_seen(self, source_id: str, state: SourceState) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO source_documents
                    (source_id, document_id, version, title, url,
                     sha256, size_bytes, published_at, last_checked_at, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now(), 'ingested')
                ON CONFLICT (source_id, document_id) DO UPDATE SET
                    version         = EXCLUDED.version,
                    title           = EXCLUDED.title,
                    url             = EXCLUDED.url,
                    sha256          = EXCLUDED.sha256,
                    size_bytes      = EXCLUDED.size_bytes,
                    published_at    = EXCLUDED.published_at,
                    last_checked_at = now(),
                    status          = 'ingested'
                """,
                (
                    source_id,
                    state.document_id,
                    state.version,
                    state.title,
                    state.url,
                    state.sha256,
                    state.size_bytes,
                    state.published_at,
                ),
            )

    def last_poll(self, source_id: str) -> datetime | None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT last_polled_at FROM source_poll_state WHERE source_id = %s",
                (source_id,),
            )
            row = cur.fetchone()
        return row[0] if row else None

    def set_last_poll(self, source_id: str, when: datetime) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO source_poll_state (source_id, last_polled_at)
                VALUES (%s, %s)
                ON CONFLICT (source_id) DO UPDATE SET last_polled_at = EXCLUDED.last_polled_at
                """,
                (source_id, when),
            )


# --------------------------------------------------------------------------- #
#  Factory
# --------------------------------------------------------------------------- #
_singleton: StateStore | None = None


def get_state_store() -> StateStore:
    """
    Return the global state store.

    Defaults to the Postgres-backed store; falls back to in-memory if the DB
    is unavailable (offline dev / unit tests without docker compose up).
    """
    global _singleton
    if _singleton is not None:
        return _singleton
    try:
        store = PostgresStateStore()
        # Smoke-test: touch the pool to fail fast if misconfigured.
        from uk_regs_knowledge.relational.connection import get_pool

        get_pool()
        _singleton = store
    except Exception:  # noqa: BLE001
        logger.info("Postgres unavailable; using InMemoryStateStore", exc_info=True)
        _singleton = InMemoryStateStore()
    return _singleton


def set_state_store(store: StateStore) -> None:
    """Override the global store (for tests)."""
    global _singleton
    _singleton = store


def reset_state_store() -> None:
    """Reset the global singleton (for tests)."""
    global _singleton
    _singleton = None
