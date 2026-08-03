"""
Evaluation metrics for retrieval, reconciliation, and extraction.

All metrics are pure functions over lists of per-case result dicts so they
can be unit-tested without any IO or network. Each harness (retrieval,
reconciliation, extraction) produces a list of case-result dicts and calls
the matching aggregator here.

Precision is the headline number in this domain: it is better to return
nothing than to return a wrong rule. Recall matters too, but we report
both so the trade-off is explicit in the report.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# --------------------------------------------------------------------------- #
#  Result containers
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class CaseResult:
    """Outcome of a single golden case."""

    case_id: str
    passed: bool
    detail: str = ""
    score: float = 0.0  # per-case metric in [0,1], e.g. rank-reciprocal or exact-match


@dataclass(slots=True)
class SuiteReport:
    """Aggregate metrics for one suite (retrieval/recon/extraction)."""

    suite: str
    total: int = 0
    passed: int = 0
    results: list[CaseResult] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        return self.passed / self.total if self.total else 0.0

    @property
    def mean_score(self) -> float:
        return sum(r.score for r in self.results) / self.total if self.total else 0.0

    def add(self, result: CaseResult) -> None:
        self.results.append(result)
        self.total += 1
        if result.passed:
            self.passed += 1


# --------------------------------------------------------------------------- #
#  Retrieval metrics
# --------------------------------------------------------------------------- #
def precision_at_k(retrieved_ids: list[str], relevant_ids: list[str], k: int) -> float:
    """
    Fraction of the top-k retrieved items that are relevant.

    Returns 0.0 if k == 0. Relevant set empty -> 0.0 (there is nothing
    relevant to be precise about).
    """
    if k <= 0:
        return 0.0
    topk = retrieved_ids[:k]
    if not topk:
        return 0.0
    hits = sum(1 for rid in topk if rid in relevant_ids)
    return hits / len(topk)


def recall_at_k(retrieved_ids: list[str], relevant_ids: list[str], k: int) -> float:
    """
    Fraction of relevant items that appear in the top-k.

    Returns 1.0 when relevant_ids is empty AND nothing was retrieved (vacuous
    correctness); 0.0 if irrelevant items were returned for a query that should
    have returned nothing.
    """
    if not relevant_ids:
        return 1.0 if not retrieved_ids[:k] else 0.0
    topk = retrieved_ids[:k]
    hits = sum(1 for rid in relevant_ids if rid in topk)
    return hits / len(relevant_ids)


def reciprocal_rank(retrieved_ids: list[str], relevant_ids: list[str]) -> float:
    """1/rank of the first relevant item, 0 if none."""
    for i, rid in enumerate(retrieved_ids, start=1):
        if rid in relevant_ids:
            return 1.0 / i
    return 0.0


def evaluate_retrieval_case(
    retrieved_ids: list[str],
    relevant_ids: list[str],
    *,
    k: int = 3,
) -> CaseResult:
    """
    A retrieval case passes if recall@k == 1.0 (all expected rules surfaced).
    Score is reciprocal rank, rewarding getting the right answer first.
    """
    rec = recall_at_k(retrieved_ids, relevant_ids, k)
    rr = reciprocal_rank(retrieved_ids, relevant_ids)
    passed = rec >= 1.0
    detail = f"recall@{k}={rec:.2f}, rr={rr:.2f}"
    return CaseResult(case_id="", passed=passed, detail=detail, score=rr)


# --------------------------------------------------------------------------- #
#  Reconciliation metrics
# --------------------------------------------------------------------------- #
def evaluate_reconciliation_case(
    predicted_decision: str,
    expected_decision: str,
    *,
    confidence: float | None = None,
    min_confidence: float = 0.0,
) -> CaseResult:
    """
    A reconciliation case passes if the predicted decision label matches the
    expected label exactly. Confidence is reported as the score so low-
    confidence correct answers are visible in the report.
    """
    match = predicted_decision.lower() == expected_decision.lower()
    score = confidence if confidence is not None else (1.0 if match else 0.0)
    detail = f"pred={predicted_decision}, expected={expected_decision}"
    if confidence is not None:
        detail += f", conf={confidence:.2f}"
    # Treat a correct-but-low-confidence answer as a soft fail in the score.
    if match and confidence is not None and confidence < min_confidence:
        score = confidence  # still recorded, but visible as low
    return CaseResult(case_id="", passed=match, detail=detail, score=score)


# --------------------------------------------------------------------------- #
#  Extraction metrics
# --------------------------------------------------------------------------- #
def _norm_measurement(m: dict[str, Any]) -> tuple[float, str]:
    """Normalize a measurement dict to a comparable (value, unit) tuple."""
    try:
        val = float(str(m.get("value", "")).replace(",", ""))
    except (TypeError, ValueError):
        val = float("nan")
    return val, str(m.get("unit", "")).lower()


def evaluate_extraction_case(
    predicted_measurements: list[dict[str, Any]],
    expected_measurements: list[dict[str, Any]],
    predicted_standards: list[str],
    expected_standards: list[str],
    predicted_sections: list[str],
    expected_sections: list[str],
    predicted_param_keys: list[str],
    expected_param_keys: list[str],
) -> CaseResult:
    """
    Extraction passes if measurements, standards, sections, and parameter keys
    all match exactly (set equality after normalization). This is strict by
    design — a missed measurement in a compliance pipeline is a real defect.
    """
    pred_m = sorted(_norm_measurement(m) for m in predicted_measurements)
    exp_m = sorted(_norm_measurement(m) for m in expected_measurements)

    pred_s = sorted(s.lower() for s in predicted_standards)
    exp_s = sorted(s.lower() for s in expected_standards)

    pred_sec = sorted(s.lower() for s in predicted_sections)
    exp_sec = sorted(s.lower() for s in expected_sections)

    pred_pk = sorted(k.lower() for k in predicted_param_keys)
    exp_pk = sorted(k.lower() for k in expected_param_keys)

    checks = {
        "measurements": pred_m == exp_m,
        "standards": pred_s == exp_s,
        "sections": pred_sec == exp_sec,
        "param_keys": pred_pk == exp_pk,
    }
    passed = all(checks.values())
    score = sum(checks.values()) / len(checks)
    failed = [k for k, ok in checks.items() if not ok]
    detail = "all match" if passed else f"mismatch: {', '.join(failed)}"
    return CaseResult(case_id="", passed=passed, detail=detail, score=score)


# --------------------------------------------------------------------------- #
#  Roll-up
# --------------------------------------------------------------------------- #
def summarize(suite: str, case_results: list[CaseResult]) -> SuiteReport:
    """Aggregate per-case results into a SuiteReport."""
    report = SuiteReport(suite=suite)
    for cr in case_results:
        report.add(cr)
    return report