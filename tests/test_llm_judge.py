"""
Unit tests for the LLM-as-a-judge eval suite.

Two layers of tests:

1. **Golden gate** — every case in ``evals/golden/llm_judge.yaml`` passes
   at 100 % (well-formed contexts). This is the regression gate.

2. **Judge unit tests** — exercise ``RubricJudge`` directly with synthetic
   positive AND negative cases, proving the judge can *catch* bad context:

   * Hallucinated measurements → low faithfulness.
   * Wrong / fake citations → low citation precision.
   * Irrelevant answers → low answer relevance.
   * Faithful paraphrases → all scores high.

Run:
    uv run --directory . python -m pytest tests/test_llm_judge.py -v
"""

from __future__ import annotations

import os

os.environ.setdefault("UK_REGS_BACKEND", "mock")

import pytest  # noqa: E402
from uk_regs_evals.golden import LLMJudgeCase, load_cases  # noqa: E402
from uk_regs_evals.harnesses import LLMJudgeHarness  # noqa: E402
from uk_regs_evals.llm_judge import (  # noqa: E402
    JudgeCase,
    JudgeScore,
    RubricJudge,
    _extract_cited_ids,
    _jaccard,
    _tokenize,
)


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #
def _make_case(
    *,
    query: str = "What is the minimum stair width?",
    source_text: str = "The minimum width of an escape stair shall be 1100 mm.",
    generated: str = "The minimum stair width is 1100 mm (ESC-110).",
    source_rule_id: str = "ESC-110",
    expected_ids: list[str] | None = None,
) -> JudgeCase:
    return JudgeCase(
        id="test",
        query=query,
        source_rules=[{"rule_id": source_rule_id, "text": source_text}],
        generated_context=generated,
        expected_rule_ids=expected_ids or [source_rule_id],
    )


# --------------------------------------------------------------------------- #
#  Tokenisation helpers
# --------------------------------------------------------------------------- #
class TestTokenise:
    def test_stops_words_removed(self):
        tokens = _tokenize("The stair shall be at least 1100 mm wide")
        assert "the" not in tokens
        assert "shall" not in tokens
        assert "1100" in tokens
        assert "mm" in tokens

    def test_units_preserved(self):
        tokens = _tokenize("pressure differential of 50 Pa")
        assert "50" in tokens
        assert "pa" in tokens

    def test_empty_string(self):
        assert _tokenize("") == set()


class TestJaccard:
    def test_identical_sets(self):
        assert _jaccard({"a", "b"}, {"a", "b"}) == 1.0

    def test_disjoint_sets(self):
        assert _jaccard({"a"}, {"b"}) == 0.0

    def test_partial_overlap(self):
        assert _jaccard({"a", "b"}, {"b", "c"}) == pytest.approx(1 / 3)


class TestExtractCitedIds:
    def test_extracts_pattern(self):
        assert _extract_cited_ids("see FIR-301 and ESC-110") == ["FIR-301", "ESC-110"]

    def test_no_citations(self):
        assert _extract_cited_ids("no citations here") == []


# --------------------------------------------------------------------------- #
#  RubricJudge — positive cases
# --------------------------------------------------------------------------- #
class TestRubricJudgePositive:
    """Well-formed contexts should score high on all dimensions."""

    def test_faithful_paraphrase_scores_high(self):
        case = _make_case(generated="The minimum escape stair width is 1100 mm (ESC-110).")
        score = RubricJudge().score(case)
        assert score.faithfulness >= 0.5
        assert score.citation_precision == 1.0
        assert score.overall > 0.5

    def test_exact_copy_scores_high(self):
        case = _make_case(
            generated="The minimum width of an escape stair shall be 1100 mm. (ESC-110)"
        )
        score = RubricJudge().score(case)
        assert score.faithfulness >= 0.7

    def test_correct_citations(self):
        case = _make_case(
            source_rule_id="FIR-301",
            expected_ids=["FIR-301"],
            generated="Fire resistance is 60 minutes (FIR-301).",
            source_text="Fire resistance rating shall be 60 minutes.",
        )
        score = RubricJudge().score(case)
        assert score.citation_precision == 1.0


# --------------------------------------------------------------------------- #
#  RubricJudge — negative cases (the judge must CATCH these)
# --------------------------------------------------------------------------- #
class TestRubricJudgeNegative:
    """Bad contexts should score low — the judge must detect them."""

    def test_hallucinated_measurement_reduces_faithfulness(self):
        """Changing 1100 mm → 9999 mm should tank faithfulness."""
        case = _make_case(
            source_text="The minimum width of an escape stair shall be 1100 mm.",
            generated="The minimum stair width is 9999 mm (ESC-110).",
            source_rule_id="ESC-110",
            expected_ids=["ESC-110"],
        )
        score = RubricJudge().score(case)
        # The hallucinated number means lower token overlap with the source.
        assert score.faithfulness < 0.9  # not a perfect faithful score

    def test_wrong_citation_zero_precision(self):
        """Citing a non-existent rule → citation precision = 0."""
        case = _make_case(
            source_rule_id="ACC-410",
            expected_ids=["ACC-410"],
            generated="The minimum headroom is 2.0 metres (ZZZ-999).",
            source_text="The minimum headroom shall be 2.0 metres.",
        )
        score = RubricJudge().score(case)
        assert score.citation_precision == 0.0

    def test_missing_citation_fails_precision(self):
        """No citation when one is expected → precision = 0."""
        case = _make_case(
            source_rule_id="ACC-410",
            expected_ids=["ACC-410"],
            generated="The minimum headroom is 2.0 metres.",  # no (ACC-410)
            source_text="The minimum headroom shall be 2.0 metres.",
        )
        score = RubricJudge().score(case)
        assert score.citation_precision == 0.0

    def test_irrelevant_answer_reduces_relevance(self):
        """Answering about stairs when asked about glazing → low relevance."""
        case = _make_case(
            query="What is the maximum glazing ratio?",
            source_text="Glazing shall not exceed 25% of floor area.",
            generated="Staircases must be protected with FD60S fire doors (FAC-620).",
            source_rule_id="FAC-620",
            expected_ids=["FAC-620"],
        )
        score = RubricJudge().score(case)
        assert score.answer_relevance < 0.3

    def test_overall_is_weakest_link(self):
        """Overall = min(faith, relevance, citation_precision)."""
        case = _make_case(
            query="glazing",
            source_text="glazing",
            generated="fire doors (ZZZ-999)",
            source_rule_id="FAC-620",
            expected_ids=["FAC-620"],
        )
        score = RubricJudge().score(case)
        assert score.overall == min(
            score.faithfulness, score.answer_relevance, score.citation_precision
        )


# --------------------------------------------------------------------------- #
#  Golden gate — every YAML case passes at 100%
# --------------------------------------------------------------------------- #
class TestLLMJudgeGoldenGate:
    """The golden YAML contains only well-formed contexts — all must pass."""

    def test_yaml_loads_correctly(self):
        cases = load_cases("llm_judge")
        assert len(cases) >= 5
        assert all(isinstance(c, LLMJudgeCase) for c in cases)
        assert all(c.query for c in cases)
        assert all(c.source_rules for c in cases)
        assert all(c.generated_context for c in cases)

    def test_golden_suite_passes_at_100_percent(self):
        """Every well-formed golden case must pass — the regression gate."""
        report = LLMJudgeHarness(judge=RubricJudge()).run()
        assert report.total == len(load_cases("llm_judge"))
        assert report.passed == report.total, (
            f"LLM-judge golden gate failed: {report.passed}/{report.total} passed. "
            f"Failures: {[r.case_id for r in report.results if not r.passed]}"
        )
        assert report.accuracy == 1.0


# --------------------------------------------------------------------------- #
#  JudgeScore dataclass
# --------------------------------------------------------------------------- #
class TestJudgeScore:
    def test_overall_is_min(self):
        s = JudgeScore(faithfulness=0.9, answer_relevance=0.5, citation_precision=1.0)
        assert s.overall == 0.5

    def test_perfect_score(self):
        s = JudgeScore(faithfulness=1.0, answer_relevance=1.0, citation_precision=1.0)
        assert s.overall == 1.0

    def test_is_frozen(self):
        s = JudgeScore(faithfulness=1.0, answer_relevance=1.0, citation_precision=1.0)
        with pytest.raises((AttributeError, TypeError)):
            s.faithfulness = 0.5  # type: ignore[misc]
