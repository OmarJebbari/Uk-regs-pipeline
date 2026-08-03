"""
Unit tests for the evals package: metrics (pure functions), the golden loader,
the harnesses (with stub backends), and an end-to-end "golden run" that proves
every golden case passes at 100% accuracy against the real pipeline components.

Run:
    uv run --directory . python -m pytest tests/test_evals.py -v
"""

from __future__ import annotations

import os
from dataclasses import dataclass

os.environ.setdefault("UK_REGS_BACKEND", "mock")

import pytest  # noqa: E402
from uk_regs_evals.golden import (  # noqa: E402
    ExtractionCase,
    ExtractionExpected,
    ReconciliationCase,
    RetrievalCase,
    load_cases,
)
from uk_regs_evals.harnesses import (  # noqa: E402
    ExtractionHarness,
    ReconciliationHarness,
    RetrievalHarness,
    run_all,
)
from uk_regs_evals.metrics import (  # noqa: E402
    evaluate_extraction_case,
    evaluate_reconciliation_case,
    evaluate_retrieval_case,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
    summarize,
)


# --------------------------------------------------------------------------- #
#  Retrieval metrics — pure functions
# --------------------------------------------------------------------------- #
class TestRetrievalMetrics:
    def test_precision_at_k(self):
        assert precision_at_k(["a", "b", "c"], ["a"], k=3) == pytest.approx(1 / 3)
        assert precision_at_k(["a", "b", "c"], ["a", "b"], k=3) == pytest.approx(2 / 3)
        assert precision_at_k([], ["a"], k=3) == 0.0
        assert precision_at_k(["a", "b"], [], k=2) == 0.0
        assert precision_at_k(["a", "b"], ["a"], k=0) == 0.0

    def test_recall_at_k(self):
        # All relevant surfaced
        assert recall_at_k(["a", "b", "c"], ["a", "b"], k=3) == 1.0
        # One of two relevant surfaced
        assert recall_at_k(["a", "x", "y"], ["a", "b"], k=3) == 0.5
        # Empty relevant set, nothing retrieved -> vacuous correctness
        assert recall_at_k([], [], k=3) == 1.0
        # Empty relevant set but junk returned -> 0.0 (a wrong result)
        assert recall_at_k(["x"], [], k=3) == 0.0

    def test_reciprocal_rank(self):
        assert reciprocal_rank(["a", "b"], ["a"]) == 1.0
        assert reciprocal_rank(["x", "a"], ["a"]) == 0.5
        assert reciprocal_rank(["x", "y", "a"], ["a"]) == pytest.approx(1 / 3)
        assert reciprocal_rank(["x", "y"], ["a"]) == 0.0

    def test_evaluate_retrieval_case_passes_when_all_relevant_surfaced(self):
        cr = evaluate_retrieval_case(["a", "b"], ["a", "b"], k=3)
        assert cr.passed
        assert cr.score == 1.0  # first relevant at rank 1

    def test_evaluate_retrieval_case_fails_on_missing_relevant(self):
        cr = evaluate_retrieval_case(["a"], ["a", "b"], k=3)
        assert not cr.passed
        assert cr.score == 1.0  # rr still rewards rank-1 hit


# --------------------------------------------------------------------------- #
#  Reconciliation metrics
# --------------------------------------------------------------------------- #
class TestReconciliationMetrics:
    def test_match_passes(self):
        cr = evaluate_reconciliation_case("novel", "novel")
        assert cr.passed
        assert cr.score == 1.0

    def test_mismatch_fails(self):
        cr = evaluate_reconciliation_case("match", "novel")
        assert not cr.passed
        assert cr.score == 0.0

    def test_confidence_recorded_as_score(self):
        cr = evaluate_reconciliation_case(
            "partial", "partial", confidence=0.72
        )
        assert cr.passed
        assert cr.score == pytest.approx(0.72)

    def test_case_insensitive(self):
        cr = evaluate_reconciliation_case("NOVEL", "novel")
        assert cr.passed


# --------------------------------------------------------------------------- #
#  Extraction metrics
# --------------------------------------------------------------------------- #
class TestExtractionMetrics:
    def test_all_match_passes(self):
        cr = evaluate_extraction_case(
            predicted_measurements=[{"value": 2000, "unit": "mm"}],
            expected_measurements=[{"value": 2000, "unit": "mm"}],
            predicted_standards=["BS EN 1991-1-1"],
            expected_standards=["bs en 1991-1-1"],
            predicted_sections=["2.3.1"],
            expected_sections=["2.3.1"],
            predicted_param_keys=["measurement_mm"],
            expected_param_keys=["measurement_mm"],
        )
        assert cr.passed
        assert cr.score == 1.0

    def test_measurement_mismatch_fails(self):
        cr = evaluate_extraction_case(
            predicted_measurements=[{"value": 1200, "unit": "mm"}],
            expected_measurements=[{"value": 2000, "unit": "mm"}],
            predicted_standards=[],
            expected_standards=[],
            predicted_sections=[],
            expected_sections=[],
            predicted_param_keys=[],
            expected_param_keys=[],
        )
        assert not cr.passed
        assert cr.score < 1.0
        assert "measurements" in cr.detail

    def test_thousands_separator_normalized(self):
        cr = evaluate_extraction_case(
            predicted_measurements=[{"value": "2,000", "unit": "mm"}],
            expected_measurements=[{"value": 2000, "unit": "mm"}],
            predicted_standards=[],
            expected_standards=[],
            predicted_sections=[],
            expected_sections=[],
            predicted_param_keys=[],
            expected_param_keys=[],
        )
        assert cr.passed

    def test_missing_standard_fails(self):
        cr = evaluate_extraction_case(
            predicted_measurements=[],
            expected_measurements=[],
            predicted_standards=[],
            expected_standards=["BS EN 1991-1-1"],
            predicted_sections=[],
            expected_sections=[],
            predicted_param_keys=[],
            expected_param_keys=[],
        )
        assert not cr.passed
        assert "standards" in cr.detail


# --------------------------------------------------------------------------- #
#  Golden loader
# --------------------------------------------------------------------------- #
class TestGoldenLoader:
    def test_retrieval_cases_load(self):
        cases = load_cases("retrieval")
        assert len(cases) > 0
        assert all(isinstance(c, RetrievalCase) for c in cases)
        assert all(c.id and c.query for c in cases)

    def test_reconciliation_cases_load(self):
        cases = load_cases("reconciliation")
        assert len(cases) > 0
        assert all(isinstance(c, ReconciliationCase) for c in cases)
        assert all(c.expected_decision for c in cases)

    def test_extraction_cases_load(self):
        cases = load_cases("extraction")
        assert len(cases) > 0
        assert all(isinstance(c, ExtractionCase) for c in cases)
        assert all(isinstance(c.expected, ExtractionExpected) for c in cases)

    def test_unknown_suite_raises(self):
        with pytest.raises(ValueError):
            load_cases("does-not-exist")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
#  Harnesses with stub backends
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class _StubHit:
    """Minimal duck-typed stand-in for a SearchHit (has .score + .rule.rule_id)."""

    score: float
    rule_id: str

    # RetrievalHarness accesses h.rule.rule_id — expose a tiny shim.
    @property
    def rule(self):
        @dataclass(slots=True)
        class _R:
            rule_id: str
        return _R(self.rule_id)


class _StubBackend:
    """Returns a fixed result set per query for deterministic tests."""

    def __init__(self, results: dict[str, list[_StubHit]] | None = None) -> None:
        self._results = results or {}

    def search(self, query: str, top_k: int = 3):
        return self._results.get(query, [])[:top_k]


class _RaisingBackend:
    """Backend that always raises — proves error handling in the harness."""

    def search(self, query: str, top_k: int = 3):
        raise RuntimeError("boom")


class TestRetrievalHarness:
    def test_all_pass_with_perfect_backend(self):
        cases = load_cases("retrieval")
        # Build a backend that returns exactly the expected rule_ids for each query.
        results = {}
        for c in cases:
            # Skip purely-negative cases (empty expected) — they pass by returning nothing.
            if not c.expected_rule_ids:
                continue
            results[c.query] = [
                _StubHit(score=1.0 - i * 0.1, rule_id=rid)
                for i, rid in enumerate(c.expected_rule_ids)
            ]
        backend = _StubBackend(results)
        report = RetrievalHarness(backend=backend).run()
        assert report.total == len(cases)
        assert report.passed == len(cases)
        assert report.accuracy == 1.0

    def test_negative_case_passes_when_nothing_returned(self):
        """A query that should return nothing passes when the backend is empty."""
        backend = _StubBackend({})
        report = RetrievalHarness(backend=backend).run()
        # Every negative case (empty expected_rule_ids) must pass.
        neg = [c for c in load_cases("retrieval") if not c.expected_rule_ids]
        if neg:
            neg_results = [
                r for r in report.results for c in neg if r.case_id == c.id
            ]
            assert all(r.passed for r in neg_results)

    def test_backend_failure_marked_as_failure(self):
        report = RetrievalHarness(backend=_RaisingBackend()).run()
        assert report.total > 0
        assert report.passed == 0
        assert all("ERROR" in r.detail for r in report.results)


class TestReconciliationHarness:
    def test_runs_clean(self):
        """The reconciliation harness executes every golden case without error."""
        report = ReconciliationHarness().run()
        assert report.total == len(load_cases("reconciliation"))
        # Every case produces a decision (no crash); score is recorded.
        assert all(r.detail for r in report.results)


class TestExtractionHarness:
    def test_runs_clean(self):
        report = ExtractionHarness().run()
        assert report.total == len(load_cases("extraction"))
        assert all(r.detail for r in report.results)


# --------------------------------------------------------------------------- #
#  End-to-end golden run
# --------------------------------------------------------------------------- #
class TestGoldenRun:
    """
    The headline test: every golden case passes at 100% accuracy when run
    against the real (deterministic) pipeline components + mock backend.

    This is the regression gate — if a refactor breaks extraction or
    reconciliation, this test fails.
    """

    def test_all_suites_at_100_percent(self):
        from uk_regs_api.service import get_backend

        report = run_all(backend=get_backend())
        assert report.overall_accuracy == 1.0, (
            f"Golden gate failed: {report.overall_accuracy:.1%} — "
            "see failures in the report."
        )
        assert report.passes_gate(1.0)

    def test_report_markdown_contains_failures_section_only_on_failure(self):
        # A passing report has no "failures" section.
        from uk_regs_api.service import get_backend

        report = run_all(backend=get_backend())
        md = report.to_markdown()
        assert "Overall accuracy" in md
        if report.overall_accuracy == 1.0:
            assert "failures" not in md.lower()


# --------------------------------------------------------------------------- #
#  Summarize helper
# --------------------------------------------------------------------------- #
class TestSummarize:
    def test_accuracy_and_mean_score(self):
        from uk_regs_evals.metrics import CaseResult

        report = summarize(
            "fake",
            [
                CaseResult("c1", passed=True, score=1.0),
                CaseResult("c2", passed=False, score=0.5),
            ],
        )
        assert report.suite == "fake"
        assert report.total == 2
        assert report.passed == 1
        assert report.accuracy == 0.5
        assert report.mean_score == 0.75

    def test_empty_report(self):
        report = summarize("empty", [])
        assert report.total == 0
        assert report.accuracy == 0.0
        assert report.mean_score == 0.0