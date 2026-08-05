"""
Tests for Phase 4B-3b: API-key auth, health probes, request logging.

Run:
    uv run --directory . python -m pytest tests/test_auth_observability.py -v
"""

from __future__ import annotations

import importlib
import os

os.environ.setdefault("UK_REGS_BACKEND", "mock")

import pytest  # noqa: E402
import uk_regs_core.config as cfg_module  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from uk_regs_api import app as app_module  # noqa: E402


@pytest.fixture
def client() -> TestClient:
    """Fresh app with no API key configured (open dev mode)."""
    # Ensure no key
    os.environ.pop("API_KEY", None)
    cfg_module.get_settings.cache_clear()
    importlib.reload(app_module)
    return TestClient(app_module.app)


@pytest.fixture
def protected_client(monkeypatch) -> TestClient:
    """App with API_KEY set — mutating routes gated."""
    monkeypatch.setenv("API_KEY", "secret-test-key")
    cfg_module.get_settings.cache_clear()
    importlib.reload(app_module)
    return TestClient(app_module.app)


# --------------------------------------------------------------------------- #
#  Health probes
# --------------------------------------------------------------------------- #
def test_healthz_returns_ok(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_health_alias_works(client):
    r = client.get("/health")
    assert r.status_code == 200


def test_readyz_returns_ok_with_mock_backend(client):
    r = client.get("/readyz")
    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is True
    assert "backend" in body["checks"]


# --------------------------------------------------------------------------- #
#  Auth: open mode (no API_KEY)
# --------------------------------------------------------------------------- #
def test_open_mode_allows_get(client):
    r = client.get("/changes")
    assert r.status_code == 200


def test_open_mode_allows_post(client):
    """Without API_KEY, POST is allowed."""
    r = client.post(
        "/changes/cs-demo-002/review",
        data={"decision": "applied", "reviewer": "t@t.com", "notes": "x"},
    )
    assert r.status_code == 200


# --------------------------------------------------------------------------- #
#  Auth: protected mode (API_KEY set)
# --------------------------------------------------------------------------- #
def test_protected_get_allowed_without_key(protected_client):
    """GET stays open even when API_KEY is set (IDE read-only access)."""
    r = protected_client.get("/changes")
    assert r.status_code == 200


def test_protected_post_rejected_without_key(protected_client):
    """POST without key → 401."""
    r = protected_client.post(
        "/changes/cs-demo-002/review",
        data={"decision": "rejected", "reviewer": "t@t.com"},
    )
    assert r.status_code == 401
    assert "api key" in r.json()["detail"].lower()


def test_protected_post_rejected_wrong_key(protected_client):
    """POST with wrong key → 401."""
    r = protected_client.post(
        "/changes/cs-demo-002/review",
        data={"decision": "rejected", "reviewer": "t@t.com"},
        headers={"X-API-Key": "wrong"},
    )
    assert r.status_code == 401


def test_protected_post_allowed_with_correct_key(protected_client):
    """POST with correct key → 200."""
    r = protected_client.post(
        "/changes/cs-demo-002/review",
        data={"decision": "rejected", "reviewer": "t@t.com"},
        headers={"X-API-Key": "secret-test-key"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "rejected"


def test_health_endpoints_open_when_protected(protected_client):
    """Health/docs bypass auth."""
    r1 = protected_client.get("/healthz")
    assert r1.status_code == 200
    r2 = protected_client.get("/readyz")
    assert r2.status_code == 200


# --------------------------------------------------------------------------- #
#  Request logging middleware (request-id propagation)
# --------------------------------------------------------------------------- #
def test_request_id_returned_in_header(client):
    """Every response carries an X-Request-ID."""
    r = client.get("/healthz")
    assert "x-request-id" in r.headers
    assert r.headers["x-request-id"]  # non-empty


def test_request_id_echoed_when_provided(client):
    """Client-supplied X-Request-ID is echoed back."""
    custom = "my-trace-123"
    r = client.get("/healthz", headers={"X-Request-ID": custom})
    assert r.headers["x-request-id"] == custom
