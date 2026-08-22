"""
Shared pytest fixtures for the UK Regs Pipeline test-suite.

Provides test isolation for the FastAPI backend singleton.

``get_backend()`` is memoised with ``functools.lru_cache`` for production
efficiency (the DB-reachability probe runs at most once per process). In the
test-suite, however, several tests mutate the ``MockBackend``'s in-memory
state (e.g. ``review_change_set`` flips a change-set's status). Without
isolation those mutations leak across test files and cause order-dependent
failures such as::

    tests/test_auth_observability.py   →  sets cs-demo-002 → "rejected"
    tests/test_review_ui.py            →  asserts cs-demo-002 == "proposed"  ✗

The autouse fixture below clears the lru_cache before *every* test so each
one starts from a freshly seeded ``MockBackend`` (or a freshly probed
``RealBackend``). ``MockBackend`` construction is trivial (3 rules, 2
change-sets built from pure functions), so the per-test overhead is
negligible (< 0.1 ms).
"""

from __future__ import annotations
  
import os
  
import pytest
  
  
@pytest.fixture(autouse=True)
def _reset_backend_singleton():
    """Reset process-level singletons before each test.
  
    Guarantees a fresh backend instance (re-seeded from ``_mock_rules`` /
    ``_mock_change_sets`` for the mock, or a new connection probe for the
    real backend) so no test observes state mutated by a previous test.
  
    Also resets the ``get_settings`` lru_cache and forces auth "open mode"
    (``API_KEY`` unset). Without this, tests that enable the protected mode
    (e.g. ``test_auth_observability.py``'s ``protected_client`` fixture via
    ``monkeypatch.setenv("API_KEY", ...)``) poison the settings cache:
    ``monkeypatch`` restores the env var on teardown but never re-clears the
    cache, so subsequent POSTs in other test files resolve a stale
    ``Settings(api_key="...")`` and fail with 401.
  
    The imports are deferred so that non-API tests (e.g. pure unit tests in
    ``test_evals.py``) are unaffected if the API package is not importable
    in their environment.
    """
    # Force deterministic open mode for every test unless the test itself
    # opts into protected mode *after* this fixture runs (monkeypatch.setenv
    # in its own fixture setup is applied later and wins).
    os.environ.pop("API_KEY", None)
    try:
        from uk_regs_core.config import get_settings
  
        get_settings.cache_clear()
    except Exception:  # noqa: BLE001 — settings not relevant for this test
        pass
    try:
        from uk_regs_api.service import get_backend
  
        get_backend.cache_clear()
    except Exception:  # noqa: BLE001 — backend not relevant for this test
        pass
    yield
    # Re-clear after the test too, so a test that enabled protected mode
    # (and re-populated the cache) cannot leak it into the next test.
    try:
        from uk_regs_core.config import get_settings
  
        get_settings.cache_clear()
    except Exception:  # noqa: BLE001
        pass
