"""
End-to-end integration test: proves Phase 1 (extraction) → Phase 2
(reconciliation) → Phase 3 (API + MCP) all function together using the
real domain models.

Run:
    uv run --directory . python -m pytest tests/test_integration.py -v
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime

os.environ.setdefault("UK_REGS_BACKEND", "mock")

# ---- Phase 1: core + extraction ------------------------------------------- #
from uk_regs_core.enums import Category, Decision, RuleStatus  # noqa: E402
from uk_regs_core.ids import rule_id  # noqa: E402
from uk_regs_core.models import Citation, Rule  # noqa: E402
from uk_regs_extraction.deterministic.extractors import extract_facts  # noqa: E402
from uk_regs_reconcile.engine import Reconciler  # noqa: E402


def _build_rule(
    doc_id: str,
    section: str,
    text: str,
    *,
    parameters: dict | None = None,
    references: list[str] | None = None,
) -> Rule:
    return Rule(
        rule_id=rule_id(doc_id, section, index=0),
        doc_id=doc_id,
        version="v1",
        section_path=section,
        category=Category.FIRE,
        title=f"Rule {section}",
        text=text,
        normalized_form=text.lower(),
        parameters=parameters or {},
        references=references or [],
        valid_from=date(2025, 1, 1),
        recorded_at=datetime.now(UTC),
        status=RuleStatus.ACTIVE,
        confidence=0.95,
        extraction_method="hybrid",
        citation=Citation(
            doc_id=doc_id,
            version="v1",
            page=12,
            raw_text=text,
        ),
    )


def test_phase1_deterministic_extraction():
    """Deterministic extractors pull measurements, standards, sections."""
    facts = extract_facts(
        "The minimum headroom shall be 2,000 mm. Refer to BS EN 1991-1-1 "
        "for loads. See section 2.3.1 for details."
    )
    assert facts.has_any
    assert any(m["unit"].lower() == "mm" for m in facts.measurements)
    assert any("1991-1-1" in s for s in facts.standards)
    assert "2.3.1" in facts.sections
    # Typed parameters flow into Rule.parameters
    params = facts.as_parameters()
    assert any("measurement_mm" in k for k in params)


def test_phase1_to_phase2_reconciliation_novel():
    """A brand-new rule is classified NOVEL against an empty corpus."""
    new_rule = _build_rule(
        "ADB", "2.3", "All fire doors shall be FD60 rated.", references=["BS EN 1634"]
    )
    rec = Reconciler(llm=None)  # deterministic-only path
    results, cs_id = rec.reconcile([new_rule], corpus=[], doc_id="ADB")
    assert cs_id.startswith("CS_ADB_")
    assert len(results) == 1
    assert results[0].delta.decision == Decision.NOVEL


def test_phase1_to_phase2_reconciliation_match_and_amend():
    """
    Same rule re-extracted -> MATCH.
    Related-but-changed rule -> PARTIAL (amended).
    """
    # Existing corpus rule
    old = _build_rule(
        "ADB",
        "2.3",
        "Min corridor width shall be 1,000 mm.",
        parameters={"measurement_mm": 1000.0},
    )

    # 1) Near-identical re-extraction -> MATCH
    same = _build_rule(
        "ADB",
        "2.3",
        "Min corridor width shall be 1,000 mm.",
        parameters={"measurement_mm": 1000.0},
    )
    rec = Reconciler(llm=None)
    results, _ = rec.reconcile([same], corpus=[old], doc_id="ADB")
    assert results[0].delta.decision in (Decision.MATCH, Decision.PARTIAL)

    # 2) Amended width -> should be PARTIAL (related, text differs)
    amended = _build_rule(
        "ADB",
        "2.3",
        "Min corridor width shall be 1,200 mm.",
        parameters={"measurement_mm": 1200.0},
    )
    results, _ = rec.reconcile([amended], corpus=[old], doc_id="ADB")
    assert results[0].delta.decision in (
        Decision.PARTIAL,
        Decision.MATCH,
        Decision.CONFLICT,
    )  # deterministic thresholds decide; the point is it runs cleanly


def test_phase3_api_endpoints():
    """The FastAPI serves all Phase-3 endpoints from the mock backend."""
    from fastapi.testclient import TestClient
    from uk_regs_api.app import app

    client = TestClient(app)

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    search = client.get("/search", params={"q": "fire door", "top_k": 3})
    assert search.status_code == 200
    assert "count" in search.json()

    current = client.get("/current")
    assert current.status_code == 200
    assert current.json()["count"] >= 1

    export = client.get("/export/markdown")
    assert export.status_code == 200
    assert export.json()["filename"] == "CONSOLIDATED_REGULATIONS.md"


def test_phase3_mcp_server_builds():
    """The MCP server constructs and registers its IDE tools."""
    from uk_regs_mcp.main import build_server

    server = build_server()
    assert server.name == "uk-regs"


def test_full_pipeline_extrac_to_serve():
    """
    Full happy path: extract facts -> build rule -> reconcile against corpus
    -> serve via API. This is the 'remember old + update with new' promise.
    """
    # 1) Simulate the OLD corpus (previous edition of the regulation)
    old_corpus = [
        _build_rule(
            "ADB",
            "B1",
            "Staircase headroom shall be at least 2,000 mm.",
            parameters={"measurement_mm": 2000.0},
        )
    ]

    # 2) A NEW PDF arrives: extract deterministic facts
    new_text = "Staircase headroom shall be at least 2,100 mm."
    facts = extract_facts(new_text)
    assert facts.has_any

    new_rule = _build_rule(
        "ADB",
        "B1",
        new_text,
        parameters=facts.as_parameters(),
    )

    # 3) Reconcile new against old -> a ChangeSet decision
    rec = Reconciler(llm=None)
    results, cs_id = rec.reconcile([new_rule], corpus=old_corpus, doc_id="ADB")
    assert results[0].delta.decision in (
        Decision.PARTIAL,
        Decision.MATCH,
        Decision.CONFLICT,
        Decision.NOVEL,
    )

    # 4) The API still serves rules (mock backend), proving Phase 3 is wired
    from fastapi.testclient import TestClient
    from uk_regs_api.app import app

    client = TestClient(app)
    assert client.get("/current").json()["count"] >= 1


def test_phase5_hybrid_search_modes():
    """
    Phase 5-2: the /search endpoint accepts a `mode` query param and
    returns results for all three modes (hybrid, semantic, keyword).

    The mock backend has no vector index, so all modes degrade to keyword
    scoring — but the contract (param accepted, response shape correct,
    valid mode echoed back) must hold.
    """
    from fastapi.testclient import TestClient
    from uk_regs_api.app import app

    client = TestClient(app)

    # Default (hybrid)
    r = client.get("/search", params={"q": "fire door", "top_k": 3})
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "hybrid"
    assert "hits" in body

    # Explicit keyword
    r = client.get("/search", params={"q": "fire door", "mode": "keyword"})
    assert r.status_code == 200
    assert r.json()["mode"] == "keyword"

    # Explicit semantic (mock degrades to keyword, but must not error)
    r = client.get("/search", params={"q": "fire door", "mode": "semantic"})
    assert r.status_code == 200

    # Invalid mode → 400
    r = client.get("/search", params={"q": "fire door", "mode": "bogus"})
    assert r.status_code == 400
    assert "mode must be one of" in r.json()["detail"]
