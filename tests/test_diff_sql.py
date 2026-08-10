"""Regression test for the psycopg 3 pool contract in ``_diff_from_sql``.

Historical bug: ``RealBackend._diff_from_sql`` used psycopg2-style
``pool.getconn()`` / ``pool.putconn()`` against a psycopg 3
``ConnectionPool``, which does not expose those methods. The exception was
masked because the graph path is tried first and only the SQL fallback
triggered the ``AttributeError`` — and the whole method was never exercised
under tests because the suite runs with ``UK_REGS_BACKEND=mock``.

These tests pin the contract:

* the pool *must* expose ``.connection()`` as a context manager
* the pool *must not* expose ``getconn`` / ``putconn`` (psycopg2 API)
* ``_diff_from_sql`` returns the expected diff dict when a supersession row
  exists, and ``None`` when it does not.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest


# --------------------------------------------------------------------------- #
#  Fake psycopg 3 pool + connection
# --------------------------------------------------------------------------- #
class FakeCursor:
    """Records the executed SQL and returns a canned ``fetchone`` result."""

    def __init__(self, row):
        self._row = row
        self.executed: list[tuple[str, tuple]] = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params or ()))

    def fetchone(self):
        return self._row

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConnection:
    def __init__(self, row):
        self._row = row

    def cursor(self):
        return FakeCursor(self._row)


class FakePsycopg3Pool:
    """Mimics ``psycopg_pool.ConnectionPool`` — the psycopg 3 contract."""

    def __init__(self, row):
        self._row = row
        self.connection_called = False

    @contextmanager
    def connection(self):
        self.connection_called = True
        yield FakeConnection(self._row)


@pytest.fixture
def patched_pool(monkeypatch):
    """Patch ``get_pool`` to return a fake psycopg 3 pool with a given row."""

    def _install(row):
        fake = FakePsycopg3Pool(row)
        # Patch where _diff_from_sql imports it.
        monkeypatch.setattr(
            "uk_regs_knowledge.relational.connection.get_pool",
            lambda: fake,
        )
        return fake

    return _install


@pytest.fixture
def real_backend_with_mock_rules():
    """A ``RealBackend`` whose repository layer is stubbed."""
    from uk_regs_api.service import RealBackend

    backend = RealBackend()
    # Bypass _ensure() — inject a mock rule repository.
    prev_rule = MagicMock()
    prev_rule.text = "Fire doors shall have 20 min resistance (FD20)."
    prev_rule.parameters = {"min_resistance_min": 20, "rating": "FD20"}
    rules = MagicMock()
    rules.get_rule_by_id.return_value = prev_rule
    backend._rules = rules
    return backend


# --------------------------------------------------------------------------- #
#  Contract assertions
# --------------------------------------------------------------------------- #
class TestPsycopg3PoolContract:
    def test_fake_pool_exposes_connection_context_manager(self, patched_pool):
        """Sanity: the fake pool matches the real psycopg 3 API surface."""
        pool = patched_pool(row=("rule-old-001", "superseded by 2026 edition"))
        assert hasattr(pool, "connection")
        assert not hasattr(pool, "getconn"), "getconn is psycopg2 API"
        assert not hasattr(pool, "putconn"), "putconn is psycopg2 API"

        with pool.connection() as conn:
            assert conn is not None
        assert pool.connection_called


# --------------------------------------------------------------------------- #
#  _diff_from_sql behaviour
# --------------------------------------------------------------------------- #
class TestDiffFromSql:
    def test_returns_diff_when_supersession_exists(
        self, patched_pool, real_backend_with_mock_rules
    ):
        backend = real_backend_with_mock_rules
        pool = patched_pool(row=("rule-fire-000", "Updated by 2026 edition"))

        result = backend._diff_from_sql("rule-fire-001")

        assert pool.connection_called, "must use the pool.connection() context manager"
        assert result is not None
        assert result["rule_id"] == "rule-fire-000"
        assert result["reason"] == "Updated by 2026 edition"
        assert result["source"] == "sql"
        assert result["text"].startswith("Fire doors")
        assert result["parameters"]["min_resistance_min"] == 20

    def test_returns_none_when_no_supersession(self, patched_pool, real_backend_with_mock_rules):
        backend = real_backend_with_mock_rules
        patched_pool(row=None)

        result = backend._diff_from_sql("rule-fire-001")

        assert result is None

    def test_returns_partial_dict_when_prev_rule_missing(
        self, patched_pool, real_backend_with_mock_rules
    ):
        """If the old rule has been deleted, we still surface the reason."""
        backend = real_backend_with_mock_rules
        backend._rules.get_rule_by_id.return_value = None
        patched_pool(row=("rule-deleted", "orphaned supersession"))

        result = backend._diff_from_sql("rule-fire-001")

        assert result == {
            "rule_id": "rule-deleted",
            "reason": "orphaned supersession",
            "source": "sql",
        }

    def test_does_not_call_legacy_psycopg2_api(
        self, patched_pool, real_backend_with_mock_rules, monkeypatch
    ):
        """Guards against regression: AttributeError must not be raised.

        With the old psycopg2 code (``pool.getconn()``), this test would fail
        with ``AttributeError: 'FakePsycopg3Pool' object has no attribute
        'getconn'``. The patched pool only exposes the psycopg 3 contract, so
        any call to the legacy API surfaces immediately.
        """
        backend = real_backend_with_mock_rules
        pool = patched_pool(row=("rule-fire-000", "reason"))

        # Must not raise.
        result = backend._diff_from_sql("rule-fire-001")

        assert result is not None
        assert pool.connection_called
