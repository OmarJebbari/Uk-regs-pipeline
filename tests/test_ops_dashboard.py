"""
Tests for the Phase 5-3 Ops Dashboard endpoints.

Covers:
  - GET /ops returns the full HTML dashboard page.
  - GET /ops/stats returns JSON with all expected fields.
  - GET /ops/stats?partial=1 returns an HTML fragment for HTMX.
  - Stats fields match the mock-backend seeded data.
  - The dashboard renders section headings and source health.

Run:
    uv run --directory . python -m pytest tests/test_ops_dashboard.py -v
"""

from __future__ import annotations

import os

os.environ.setdefault("UK_REGS_BACKEND", "mock")

from fastapi.testclient import TestClient  # noqa: E402
from uk_regs_api.app import app  # noqa: E402

client = TestClient(app)


# --------------------------------------------------------------------------- #
#  GET /ops — full dashboard (HTML)
# --------------------------------------------------------------------------- #
def test_ops_dashboard_renders():
    r = client.get("/ops")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    # Key section headings are present
    assert "Operations Dashboard" in r.text
    assert "Active Rules" in r.text
    assert "Rules by Status" in r.text
    assert "Review Queue" in r.text


def test_ops_dashboard_has_refresh_button():
    r = client.get("/ops")
    assert r.status_code == 200
    assert "hx-get" in r.text  # HTMX refresh button
    assert "/ops/stats?partial=1" in r.text


# --------------------------------------------------------------------------- #
#  GET /ops/stats — JSON
# --------------------------------------------------------------------------- #
def test_ops_stats_json_has_all_fields():
    r = client.get("/ops/stats")
    assert r.status_code == 200
    assert "application/json" in r.headers["content-type"]
    body = r.json()

    expected_keys = {
        "generated_at",
        "active_rules",
        "total_rules",
        "rules_by_status",
        "rules_by_category",
        "documents",
        "document_versions",
        "change_sets_by_status",
        "pending_review",
        "sources_count",
        "sources_enabled",
    }
    assert expected_keys.issubset(body.keys())


def test_ops_stats_json_has_seeded_counts():
    """The mock backend seeds rules + change-sets, so counts must be > 0."""
    r = client.get("/ops/stats")
    assert r.status_code == 200
    body = r.json()

    assert body["active_rules"] > 0
    assert body["total_rules"] >= body["active_rules"]
    assert isinstance(body["rules_by_status"], dict)
    assert isinstance(body["rules_by_category"], dict)
    # pending_review is a count of proposed change-sets
    assert body["pending_review"] >= 0


# --------------------------------------------------------------------------- #
#  GET /ops/stats?partial=1 — HTMX HTML fragment
# --------------------------------------------------------------------------- #
def test_ops_stats_partial_returns_html():
    r = client.get("/ops/stats", params={"partial": "1"})
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    # Fragment renders the tile grid
    assert "ops-tile" in r.text
    assert "Active Rules" in r.text


def test_ops_stats_partial_has_no_outer_html_shell():
    """A partial should NOT include <html> or <body> (it's a fragment)."""
    r = client.get("/ops/stats", params={"partial": "1"})
    assert r.status_code == 200
    assert "<html" not in r.text.lower()
    assert "<!doctype" not in r.text.lower()


# --------------------------------------------------------------------------- #
#  Consistency between JSON and HTML
# --------------------------------------------------------------------------- #
def test_json_and_dashboard_are_consistent():
    """The active_rules count from JSON should appear in the dashboard HTML."""
    stats = client.get("/ops/stats").json()
    html = client.get("/ops").text
    assert str(stats["active_rules"]) in html
