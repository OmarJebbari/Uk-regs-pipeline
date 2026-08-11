"""
Unit tests for status gating — the contract that only ACTIVE rules are served.

Two layers are tested:
  1. MockBackend: search() and get_current() must exclude non-ACTIVE rules
     (mirrors the SQL backend's `WHERE status = 'active'`).
  2. IngestPipeline._persist_rules: when apply=True, rules below the confidence
     threshold must stay PROPOSED (never become ACTIVE), so they are excluded
     from serving until reviewed.

Run:  pytest tests/test_status_gating.py -v
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import MagicMock, patch

import pytest
from uk_regs_api.service import MockBackend
from uk_regs_core import Category, Citation, Rule, RuleStatus


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #
def _make_rule(
    rule_id: str = "rule-test-001",
    *,
    status: RuleStatus = RuleStatus.PROPOSED,
    confidence: float = 0.95,
    text: str = "fire door minimum resistance 30 minutes fd30",
    category: Category = Category.FIRE,
) -> Rule:
    """Create a rule. Default status is PROPOSED — matching how extraction
    always produces rules (both ``to_rule()`` and ``_det_only_rule()`` set
    PROPOSED). ``_persist_rules(apply=True)`` then promotes high-confidence
    ones to ACTIVE.
    """
    now = datetime.now(UTC)
    return Rule(
        rule_id=rule_id,
        doc_id="ad-b-2026",
        version="2026-ed1",
        section_path="AD-B/v2/2/2.3/b",
        category=category,
        title="Test rule",
        text=text,
        normalized_form=text.lower(),
        parameters={"min_resistance_min": 30},
        references=["BS EN 1634-1"],
        valid_from=date.today(),
        valid_to=None,
        recorded_at=now,
        status=status,
        citation=Citation(doc_id="ad-b-2026", version="2026-ed1", page=1, raw_text=text),
        confidence=confidence,
        extraction_method="hybrid",
    )


# --------------------------------------------------------------------------- #
#  MockBackend: status filter
# --------------------------------------------------------------------------- #
class TestMockBackendStatusFilter:
    """The mock must exclude non-ACTIVE rules just like the SQL backend."""

    def test_search_excludes_proposed_rules(self):
        """A PROPOSED rule must never appear in search results."""
        backend = MockBackend()
        # Inject a PROPOSED rule into the mock's in-memory list
        proposed = _make_rule(
            rule_id="rule-proposed-001",
            status=RuleStatus.PROPOSED,
            text="fire door PROPOSED secret keyword zzz",
        )
        backend._rules.append(proposed)

        hits = backend.search("fire door PROPOSED secret keyword zzz")
        ids = [h.rule.rule_id for h in hits]
        assert "rule-proposed-001" not in ids, "PROPOSED rule leaked into search results"

    def test_search_excludes_superseded_rules(self):
        """A SUPERSEDED rule must never appear in search results."""
        backend = MockBackend()
        superseded = _make_rule(
            rule_id="rule-superseded-001",
            status=RuleStatus.SUPERSEDED,
            text="superseded old fire rule xyz",
        )
        backend._rules.append(superseded)

        hits = backend.search("superseded old fire rule xyz")
        ids = [h.rule.rule_id for h in hits]
        assert "rule-superseded-001" not in ids

    def test_get_current_excludes_non_active_rules(self):
        """get_current() must only return ACTIVE rules."""
        backend = MockBackend()
        active_count_before = len(backend.get_current())

        backend._rules.append(_make_rule(rule_id="rule-prop-2", status=RuleStatus.PROPOSED))
        backend._rules.append(_make_rule(rule_id="rule-super-2", status=RuleStatus.SUPERSEDED))
        backend._rules.append(_make_rule(rule_id="rule-withdrawn-2", status=RuleStatus.WITHDRAWN))

        active_count_after = len(backend.get_current())
        assert active_count_after == active_count_before, "get_current() included non-ACTIVE rules"

    def test_get_current_with_category_still_filters_status(self):
        """Category filter must not bypass the status filter."""
        backend = MockBackend()
        proposed_fire = _make_rule(
            rule_id="rule-prop-fire",
            status=RuleStatus.PROPOSED,
            category=Category.FIRE,
        )
        backend._rules.append(proposed_fire)

        fire_rules = backend.get_current(category="fire")
        ids = [r.rule_id for r in fire_rules]
        assert "rule-prop-fire" not in ids

    def test_active_rules_still_searchable(self):
        """Sanity check: ACTIVE rules ARE in search results."""
        backend = MockBackend()
        hits = backend.search("fire door")
        ids = [h.rule.rule_id for h in hits]
        assert "rule-fire-001" in ids  # from _mock_rules()


# --------------------------------------------------------------------------- #
#  Pipeline: confidence gating in _persist_rules
# --------------------------------------------------------------------------- #
class TestConfidenceGating:
    """When apply=True, rules below threshold stay PROPOSED."""

    def test_high_confidence_rule_becomes_active(self):
        """A rule at or above the threshold is activated."""
        from uk_regs_ingest.pipeline import IngestPipeline

        pipe = IngestPipeline()
        high_conf = _make_rule(rule_id="r-high", confidence=0.95)

        mock_repo = MagicMock()
        saved: list[Rule] = []

        def capture_upsert(rule, embedding=None):
            saved.append(rule)

        mock_repo.upsert_rule.side_effect = capture_upsert

        with patch("uk_regs_ingest.pipeline.RuleRepository", return_value=mock_repo):
            pipe._persist_rules([high_conf], apply=True, threshold=0.85)

        assert saved[0].status == RuleStatus.ACTIVE

    def test_low_confidence_rule_stays_proposed(self):
        """A rule below the threshold does NOT become ACTIVE."""
        from uk_regs_ingest.pipeline import IngestPipeline

        pipe = IngestPipeline()
        low_conf = _make_rule(rule_id="r-low", confidence=0.60)

        mock_repo = MagicMock()
        saved: list[Rule] = []

        def capture_upsert(rule, embedding=None):
            saved.append(rule)

        mock_repo.upsert_rule.side_effect = capture_upsert

        with patch("uk_regs_ingest.pipeline.RuleRepository", return_value=mock_repo):
            pipe._persist_rules([low_conf], apply=True, threshold=0.85)

        assert saved[0].status == RuleStatus.PROPOSED, (
            "Low-confidence rule was activated despite threshold gate"
        )

    def test_mixed_batch_activates_only_above_threshold(self):
        """A batch with mixed confidence: only the high one activates."""
        from uk_regs_ingest.pipeline import IngestPipeline

        pipe = IngestPipeline()
        rules = [
            _make_rule(rule_id="r-low-1", confidence=0.50),
            _make_rule(rule_id="r-low-2", confidence=0.70),
            _make_rule(rule_id="r-high-1", confidence=0.90),
            _make_rule(rule_id="r-high-2", confidence=0.99),
        ]

        mock_repo = MagicMock()
        saved: list[Rule] = []

        def capture_upsert(rule, embedding=None):
            saved.append(rule)

        mock_repo.upsert_rule.side_effect = capture_upsert

        with patch("uk_regs_ingest.pipeline.RuleRepository", return_value=mock_repo):
            pipe._persist_rules(rules, apply=True, threshold=0.85)

        statuses = {r.rule_id: r.status for r in saved}
        assert statuses["r-low-1"] == RuleStatus.PROPOSED
        assert statuses["r-low-2"] == RuleStatus.PROPOSED
        assert statuses["r-high-1"] == RuleStatus.ACTIVE
        assert statuses["r-high-2"] == RuleStatus.ACTIVE

    def test_apply_false_leaves_all_proposed(self):
        """When apply=False (default), everything stays PROPOSED regardless."""
        from uk_regs_ingest.pipeline import IngestPipeline

        pipe = IngestPipeline()
        rules = [
            _make_rule(rule_id="r-1", confidence=0.99),
            _make_rule(rule_id="r-2", confidence=0.50),
        ]

        mock_repo = MagicMock()
        saved: list[Rule] = []

        def capture_upsert(rule, embedding=None):
            saved.append(rule)

        mock_repo.upsert_rule.side_effect = capture_upsert

        with patch("uk_regs_ingest.pipeline.RuleRepository", return_value=mock_repo):
            pipe._persist_rules(rules, apply=False, threshold=0.85)

        assert all(r.status == RuleStatus.PROPOSED for r in saved)

    def test_threshold_boundary_is_inclusive(self):
        """A rule exactly AT the threshold should be activated (>= comparison)."""
        from uk_regs_ingest.pipeline import IngestPipeline

        pipe = IngestPipeline()
        boundary = _make_rule(rule_id="r-boundary", confidence=0.85)

        mock_repo = MagicMock()
        saved: list[Rule] = []

        def capture_upsert(rule, embedding=None):
            saved.append(rule)

        mock_repo.upsert_rule.side_effect = capture_upsert

        with patch("uk_regs_ingest.pipeline.RuleRepository", return_value=mock_repo):
            pipe._persist_rules([boundary], apply=True, threshold=0.85)

        assert saved[0].status == RuleStatus.ACTIVE


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
