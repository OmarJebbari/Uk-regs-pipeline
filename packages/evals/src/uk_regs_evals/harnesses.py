"""
Evaluation harnesses that exercise the real pipeline components against the
golden datasets.

Three harnesses, one per suite:

  * ``RetrievalHarness``      — hits a ``ContextBackend.search()`` and checks
                                 that the expected rule_ids surface in the
                                 top-k. Uses MockBackend by default so the
                                 suite runs with zero infrastructure; flip to
                                 RealBackend by setting UK_REGS_BACKEND.
  * ``ReconciliationHarness`` — builds ``Rule`` objects from the golden dicts,
                                 runs them through the deterministic
                                 ``Reconciler`` (no LLM), and checks the
                                 decision label.
  * ``ExtractionHarness``     — runs ``extract_facts()`` on the raw text and
                                 checks measurements / standards / sections /
                                 parameter keys are recovered exactly.

Each harness returns a ``SuiteReport``. ``run_all()`` runs every suite and
prints a Markdown report; the CLI exits non-zero if accuracy falls below a
gate (default 1.0 — every golden case must pass for a compliance pipeline).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Protocol

from uk_regs_core import Category, Citation, Rule, RuleStatus
from uk_regs_extraction.deterministic.extractors import extract_facts
from uk_regs_reconcile.engine import Reconciler

from uk_regs_evals.golden import (
    ExtractionCase,
    ReconciliationCase,
    RetrievalCase,
    load_cases,
)
from uk_regs_evals.metrics import (
    CaseResult,
    SuiteReport,
    evaluate_extraction_case,
    evaluate_reconciliation_case,
    evaluate_retrieval_case,
    summarize,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
#  Search backend protocol (so the retrieval harness can be unit-tested with
#  a stub instead of the real MockBackend).
# --------------------------------------------------------------------------- #
class SearchBackend(Protocol):
    def search(self, query: str, top_k: int = ...) -> list: ...


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #
_FIXED_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_FIXED_TODAY = date(2026, 1, 1)


def _rule_from_dict(
    d: dict,
    *,
    rule_id: str,
    category: Category = Category.FIRE,
) -> Rule:
    """Build a minimal but valid Rule from a golden-suite dict."""
    text = str(d.get("text", ""))
    return Rule(
        rule_id=rule_id,
        doc_id="eval-doc",
        version="eval-v1",
        section_path=str(d.get("section_path", "")),
        category=category,
        title=text[:60],
        text=text,
        normalized_form=text.lower(),
        parameters=dict(d.get("parameters", {})),
        valid_from=_FIXED_TODAY,
        recorded_at=_FIXED_NOW,
        status=RuleStatus.ACTIVE,
        citation=Citation(
            doc_id="eval-doc",
            version="eval-v1",
            page=1,
            raw_text=text,
        ),
        confidence=1.0,
        extraction_method="eval",
    )


# --------------------------------------------------------------------------- #
#  Retrieval harness
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class RetrievalHarness:
    backend: SearchBackend
    top_k: int = 3
    # Hits below this score are dropped before scoring. Models the behaviour of
    # a real vector store, which returns nothing for unrelated queries — unlike
    # MockBackend, whose fallback returns zero-score results.
    min_score: float = 1e-6

    def run(self) -> SuiteReport:
        cases: list[RetrievalCase] = load_cases("retrieval")
        results: list[CaseResult] = []
        for c in cases:
            try:
                hits = self.backend.search(c.query, top_k=self.top_k)
                # Drop zero/near-zero hits so negative cases behave correctly.
                hits = [h for h in hits if h.score >= self.min_score]
                retrieved_ids = [h.rule.rule_id for h in hits]
            except Exception as e:  # backend failure is a hard fail for the case
                logger.exception("retrieval backend failed for %s", c.id)
                results.append(
                    CaseResult(case_id=c.id, passed=False, detail=f"ERROR: {e}", score=0.0)
                )
                continue
            cr = evaluate_retrieval_case(retrieved_ids, c.expected_rule_ids, k=self.top_k)
            cr.case_id = c.id
            results.append(cr)
        return summarize("retrieval", results)


# --------------------------------------------------------------------------- #
#  Reconciliation harness
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class ReconciliationHarness:
    # No LLM by default — the golden suite targets the deterministic path.
    reconciler: Reconciler | None = None

    def run(self) -> SuiteReport:
        cases: list[ReconciliationCase] = load_cases("reconciliation")
        rec = self.reconciler or Reconciler(llm=None)
        results: list[CaseResult] = []

        for c in cases:
            new_rule = _rule_from_dict(c.new_rule, rule_id="new-rule")
            corpus = (
                [_rule_from_dict(c.corpus_rule, rule_id="corpus-rule")] if c.corpus_rule else []
            )

            try:
                outcomes, _ = rec.reconcile([new_rule], corpus, doc_id="eval-doc")
                predicted = outcomes[0].delta.decision.value
                conf = outcomes[0].delta.confidence
            except Exception as e:
                logger.exception("reconciler failed for %s", c.id)
                results.append(
                    CaseResult(case_id=c.id, passed=False, detail=f"ERROR: {e}", score=0.0)
                )
                continue

            cr = evaluate_reconciliation_case(predicted, c.expected_decision, confidence=conf)
            cr.case_id = c.id
            results.append(cr)
        return summarize("reconciliation", results)


# --------------------------------------------------------------------------- #
#  Extraction harness
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class ExtractionHarness:
    def run(self) -> SuiteReport:
        cases: list[ExtractionCase] = load_cases("extraction")
        results: list[CaseResult] = []

        for c in cases:
            try:
                facts = extract_facts(c.text)
                pred_params = list(facts.as_parameters().keys())
            except Exception as e:
                logger.exception("extract_facts failed for %s", c.id)
                results.append(
                    CaseResult(case_id=c.id, passed=False, detail=f"ERROR: {e}", score=0.0)
                )
                continue

            cr = evaluate_extraction_case(
                predicted_measurements=facts.measurements,
                expected_measurements=c.expected.measurements,
                predicted_standards=facts.standards,
                expected_standards=c.expected.standards,
                predicted_sections=facts.sections,
                expected_sections=c.expected.sections,
                predicted_param_keys=pred_params,
                expected_param_keys=c.expected.parameter_keys,
            )
            cr.case_id = c.id
            results.append(cr)
        return summarize("extraction", results)


# --------------------------------------------------------------------------- #
#  Runner + report
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class EvalReport:
    retrieval: SuiteReport | None
    reconciliation: SuiteReport | None
    extraction: SuiteReport | None

    def all_reports(self) -> list[SuiteReport]:
        return [r for r in (self.retrieval, self.reconciliation, self.extraction) if r]

    @property
    def overall_accuracy(self) -> float:
        reports = self.all_reports()
        if not reports:
            return 0.0
        total = sum(r.total for r in reports)
        passed = sum(r.passed for r in reports)
        return passed / total if total else 0.0

    def passes_gate(self, gate: float = 1.0) -> bool:
        return self.overall_accuracy >= gate

    def to_markdown(self) -> str:
        lines = ["# Eval Report", ""]
        lines.append("| Suite | Total | Passed | Accuracy | Mean Score |")
        lines.append("|-------|-------|--------|----------|------------|")
        for r in self.all_reports():
            lines.append(
                f"| {r.suite} | {r.total} | {r.passed} | {r.accuracy:.1%} | {r.mean_score:.3f} |"
            )
        lines.append("")
        lines.append(f"**Overall accuracy:** {self.overall_accuracy:.1%}")
        lines.append("")
        # Per-case detail for failures
        for r in self.all_reports():
            failures = [c for c in r.results if not c.passed]
            if failures:
                lines.append(f"## {r.suite} — failures")
                for c in failures:
                    lines.append(f"- `{c.case_id}` — {c.detail}")
                lines.append("")
        return "\n".join(lines)


def _default_search_backend() -> SearchBackend:
    """Return the configured backend (mock by default; real if DB is up)."""
    from uk_regs_api.service import get_backend

    return get_backend()


def run_all(
    *,
    backend: SearchBackend | None = None,
    run_retrieval: bool = True,
    run_reconciliation: bool = True,
    run_extraction: bool = True,
) -> EvalReport:
    """
    Run every enabled suite and return a combined report.

    Suits can be skipped (e.g. ``run_retrieval=False`` to skip a live-DB
    retrieval pass in CI without a populated index).
    """
    retrieval = None
    reconciliation = None
    extraction = None

    if run_retrieval:
        b = backend or _default_search_backend()
        retrieval = RetrievalHarness(backend=b).run()

    if run_reconciliation:
        reconciliation = ReconciliationHarness().run()

    if run_extraction:
        extraction = ExtractionHarness().run()

    return EvalReport(
        retrieval=retrieval,
        reconciliation=reconciliation,
        extraction=extraction,
    )
