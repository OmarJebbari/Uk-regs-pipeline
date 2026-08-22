"""Tests for the Prometheus /metrics endpoint and request instrumentation."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_metrics_endpoint_exposes_prometheus_format(monkeypatch):
    """GET /metrics returns 200 with Prometheus text exposition payloads."""
    from uk_regs_api.app import app

    monkeypatch.setenv("UK_REGS_BACKEND", "mock")
    client = TestClient(app)

    resp = client.get("/metrics")
    assert resp.status_code == 200
    body = resp.text
    # Our custom metrics must be present
    assert "uk_regs_api_requests_total" in body
    assert "uk_regs_api_request_duration_seconds" in body
    # Standard process collectors come along for free
    assert "python_info" in body or "process_" in body


def test_metrics_endpoint_records_requests(monkeypatch):
    """Hitting a route increments the request counter with a templated route label."""
    from uk_regs_api.app import app

    monkeypatch.setenv("UK_REGS_BACKEND", "mock")
    client = TestClient(app)

    body_before = client.get("/metrics").text
    before = None
    for line in body_before.splitlines():
        if line.startswith("uk_regs_api_requests_total{"):
            if 'route="/healthz"' in line and 'method="GET"' in line:
                before = float(line.rsplit(" ", 1)[1])

    client.get("/healthz")

    body_after = client.get("/metrics").text
    found = None
    for line in body_after.splitlines():
        if line.startswith("uk_regs_api_requests_total{"):
            if 'route="/healthz"' in line and 'method="GET"' in line:
                found = float(line.rsplit(" ", 1)[1])

    assert found is not None, "expected a requests_total series labelled route=/healthz"
    if before is not None:
        assert found >= before + 1


def test_metrics_endpoint_exempt_from_api_key(monkeypatch):
    """Even when API_KEY is set, /metrics stays scrapeable (infra-side network policy
    is expected to restrict who can reach it)."""
    import uk_regs_core.config as cfg_module
    from uk_regs_api.app import app

    monkeypatch.setenv("UK_REGS_BACKEND", "mock")
    monkeypatch.setenv("API_KEY", "super-secret")
    cfg_module.get_settings.cache_clear()
    client = TestClient(app)

    assert client.get("/metrics").status_code == 200
    # Sanity: mutating routes do reject without the key
    assert client.post("/sources/poll").status_code == 401
