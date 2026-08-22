"""Prometheus instrumentation for the UK Regs API.

Exposes a dedicated registry with:

- ``uk_regs_api_requests_total{method,route,status}`` — request counter
- ``uk_regs_api_request_duration_seconds{method,route}`` — latency histogram
- default Python/process collectors (GC, memory, fds, ...)

The ``/metrics`` endpoint renders this registry in the Prometheus text
exposition format. Label cardinality is bounded by using the *route template*
(e.g. ``/rules/{rule_id}``) rather than the raw request path; unmatched paths
are bucketed under ``<unmatched>``.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest
from prometheus_client.exposition import CONTENT_TYPE_LATEST
from prometheus_client.platform_collector import PlatformCollector

#: Dedicated registry (keeps our metrics isolated from any default registry).
REGISTRY = CollectorRegistry(auto_describe=True)

# Standard process/platform collectors (memory, fds, python version, ...).
try:  # pragma: no cover - depends on platform
    from prometheus_client import GCCollector

    GCCollector(registry=REGISTRY)
except Exception:  # noqa: BLE001 - GC stats unavailable on some platforms
    pass
PlatformCollector(registry=REGISTRY)

REQUESTS_TOTAL: Counter = Counter(
    "uk_regs_api_requests_total",
    "Total HTTP requests handled by the API.",
    labelnames=["method", "route", "status"],
    registry=REGISTRY,
)

REQUEST_DURATION_SECONDS: Histogram = Histogram(
    "uk_regs_api_request_duration_seconds",
    "HTTP request latency in seconds.",
    labelnames=["method", "route"],
    registry=REGISTRY,
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

#: Placeholder label for paths that match no registered route (404s).
UNMATCHED_ROUTE = "<unmatched>"


def record_request(method: str, route: str, status: int, duration_s: float) -> None:
    """Record one completed request (counter tick + latency observation)."""
    REQUESTS_TOTAL.labels(method=method, route=route, status=str(status)).inc()
    REQUEST_DURATION_SECONDS.labels(method=method, route=route).observe(duration_s)


def metrics_payload() -> tuple[bytes, str]:
    """Render the registry — returns ``(body, content_type)`` for the endpoint."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST