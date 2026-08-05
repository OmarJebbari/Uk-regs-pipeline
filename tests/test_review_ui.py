"""
Tests for the Phase 4B-3a ChangeSet Review UI + REST endpoints.

Covers:
  - Dashboard lists seeded change-sets and renders counts.
  - Detail page renders deltas with side-by-side rules.
  - REST review endpoint updates status.
  - HTMX review form posts and re-renders with a flash message.
  - Invalid decision is rejected.
  - 404 on unknown change-set.

Run:
    uv run --directory . python -m pytest tests/test_review_ui.py -v
"""

from __future__ import annotations

import os

os.environ.setdefault("UK_REGS_BACKEND", "mock")

from fastapi.testclient import TestClient  # noqa: E402
from uk_regs_api.app import app  # noqa: E402

client = TestClient(app)


# --------------------------------------------------------------------------- #
#  JSON API
# --------------------------------------------------------------------------- #
def test_list_changes_returns_seeded_data():
    r = client.get("/changes")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] >= 2
    ids = {c["change_set_id"] for c in body["change_sets"]}
    assert {"cs-demo-001", "cs-demo-002"}.issubset(ids)


def test_get_change_set_returns_deltas():
    r = client.get("/changes/cs-demo-002")
    assert r.status_code == 200
    cs = r.json()
    assert cs["change_set_id"] == "cs-demo-002"
    assert cs["status"] == "proposed"
    assert len(cs["deltas"]) == 2


def test_get_change_set_404_on_unknown():
    r = client.get("/changes/does-not-exist")
    assert r.status_code == 404


def test_review_change_set_via_rest():
    """POST /changes/{id}/review flips status to applied."""
    r = client.post(
        "/changes/cs-demo-002/review",
        data={"decision": "applied", "reviewer": "tester@example.com", "notes": "lgtm"},
    )
    assert r.status_code == 200
    cs = r.json()
    assert cs["status"] == "applied"
    assert cs["reviewed_by"] == "tester@example.com"


def test_review_rejects_invalid_decision():
    r = client.post(
        "/changes/cs-demo-001/review",
        data={"decision": "maybe", "reviewer": "x"},
    )
    assert r.status_code in (400, 422)


# --------------------------------------------------------------------------- #
#  Review UI (Jinja2 + HTMX)
# --------------------------------------------------------------------------- #
def test_dashboard_renders():
    r = client.get("/ui/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    # seeded ids appear in the markup
    assert "cs-demo-001" in r.text
    assert "cs-demo-002" in r.text
    # counts section renders a Pending stat
    assert "Pending" in r.text


def test_detail_page_renders_side_by_side():
    r = client.get("/ui/changes/cs-demo-002")
    assert r.status_code == 200
    assert "Previous Rule" in r.text or "No prior rule" in r.text
    assert "New Rule" in r.text
    # the review form buttons are present
    assert "Apply" in r.text
    assert "Reject" in r.text


def test_detail_404_on_unknown():
    r = client.get("/ui/changes/nope")
    assert r.status_code == 404


def test_htmx_review_submit_rerenders_with_flash():
    """The HTMX POST endpoint re-renders detail.html and shows a flash message."""
    # cs-demo-002 was marked applied above; roll it back via the UI flow.
    r = client.post(
        "/ui/changes/cs-demo-002/review",
        data={"decision": "rolled_back", "reviewer": "ui-tester", "notes": "revert"},
    )
    assert r.status_code == 200
    assert "rolled_back" in r.text
    assert "flash" in r.text  # CSS class on the flash banner
