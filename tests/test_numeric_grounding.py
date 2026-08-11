"""
Golden-case tests for numeric parameter grounding in the LLM rule extractor.

These verify that ``_ground_parameters``:
  1. Keeps numeric values that appear in the source text (grounded).
  2. Drops numeric values that do NOT appear in the source text (hallucinations).
  3. Applies a confidence penalty proportional to the number of ungrounded values.
  4. Leaves non-numeric (qualitative) string parameters untouched.
  5. Compounds penalties for multiple violations.

Run:  pytest tests/test_numeric_grounding.py -v
"""

from __future__ import annotations

from uk_regs_extraction.deterministic.extractors import DeterministicFacts
from uk_regs_extraction.llm.rule_extractor import ExtractedRule, _ground_parameters


def _facts(measurements: list[dict] | None = None) -> DeterministicFacts:
    return DeterministicFacts(
        measurements=measurements or [],
        standards=[],
        sections=[],
    )


def _rule(
    *,
    parameters: dict | None = None,
    confidence: float = 0.95,
) -> ExtractedRule:
    return ExtractedRule(
        is_regulation=True,
        category="fire",
        title="Test",
        text="fire door resistance",
        normalized_form="fire door resistance",
        parameters=parameters or {},
        references=[],
        confidence=confidence,
        rationale="test",
    )


class TestGroundedValues:
    """Values present in the source text must survive grounding."""

    def test_numeric_value_in_text_is_kept(self):
        """A number that appears verbatim in the text is grounded."""
        rule = _rule(parameters={"min_resistance_min": 30}, confidence=0.95)
        source = "The fire door shall provide 30 minutes of resistance."
        grounded = _ground_parameters(rule, _facts(), source)
        assert grounded.parameters == {"min_resistance_min": 30}
        assert grounded.confidence == 0.95  # no penalty

    def test_numeric_value_from_facts_is_kept(self):
        """A number from deterministic measurements is grounded even if the
        raw text uses a different formatted form (e.g., '1,200' vs '1200')."""
        rule = _rule(parameters={"measurement_mm": 1200.0}, confidence=0.90)
        facts = _facts(measurements=[{"value": "1,200", "unit": "mm"}])
        source = "The corridor width is 1,200 mm."
        grounded = _ground_parameters(rule, facts, source)
        assert grounded.parameters == {"measurement_mm": 1200.0}
        assert grounded.confidence == 0.90

    def test_multiple_grounded_values_all_kept(self):
        """Multiple grounded values, no penalty."""
        rule = _rule(
            parameters={"height_m": 5, "width_m": 3},
            confidence=0.88,
        )
        source = "The ceiling height is 5 m and the width is 3 m."
        grounded = _ground_parameters(rule, _facts(), source)
        assert len(grounded.parameters) == 2
        assert grounded.confidence == 0.88

    def test_decimal_value_is_kept(self):
        """Decimal values like 1.5 must match correctly."""
        rule = _rule(parameters={"area_m2": 1.5}, confidence=0.92)
        source = "The minimum area is 1.5 m2."
        grounded = _ground_parameters(rule, _facts(), source)
        assert grounded.parameters == {"area_m2": 1.5}


class TestHallucinatedValues:
    """Values NOT in the source text must be dropped + penalized."""

    def test_hallucinated_value_is_dropped_and_penalized(self):
        """A number that doesn't appear in text is dropped, confidence reduced."""
        rule = _rule(parameters={"min_resistance_min": 999}, confidence=0.95)
        source = "The fire door shall provide 30 minutes of resistance."
        grounded = _ground_parameters(rule, _facts(), source)
        assert grounded.parameters == {}  # hallucination removed
        assert grounded.confidence < 0.95
        assert grounded.confidence == 0.80  # 0.95 - 0.15

    def test_mixed_grounded_and_hallucinated(self):
        """One grounded, one hallucinated: keep the good, drop the bad."""
        rule = _rule(
            parameters={"good_value": 30, "bad_value": 999},
            confidence=0.90,
        )
        source = "The door must resist fire for 30 minutes."
        grounded = _ground_parameters(rule, _facts(), source)
        assert "good_value" in grounded.parameters
        assert "bad_value" not in grounded.parameters
        assert grounded.confidence == 0.75  # 0.90 - 0.15

    def test_multiple_hallucinations_compound_penalty(self):
        """Two hallucinated values => 2x penalty."""
        rule = _rule(
            parameters={"bad1": 100, "bad2": 200},
            confidence=0.90,
        )
        source = "No numbers here except the word three."
        grounded = _ground_parameters(rule, _facts(), source)
        assert grounded.parameters == {}
        assert grounded.confidence == 0.60  # 0.90 - 2 * 0.15

    def test_penalty_never_goes_negative(self):
        """Confidence clamped at 0.0 even with many hallucinations."""
        rule = _rule(
            parameters={"b1": 1, "b2": 2, "b3": 3, "b4": 4, "b5": 5, "b6": 6, "b7": 7},
            confidence=0.50,
        )
        source = "There are zero numbers here."
        grounded = _ground_parameters(rule, _facts(), source)
        assert grounded.confidence == 0.0
        assert grounded.parameters == {}


class TestNonNumericValues:
    """Qualitative (non-numeric) string parameters are passed through."""

    def test_pure_string_parameter_is_kept(self):
        """A string with no digits (e.g. 'FD30') is treated as qualitative."""
        rule = _rule(parameters={"door_rating": "standard"}, confidence=0.90)
        source = "Some source text without numbers."
        grounded = _ground_parameters(rule, _facts(), source)
        assert grounded.parameters == {"door_rating": "standard"}
        assert grounded.confidence == 0.90

    def test_empty_parameters_noop(self):
        """No parameters => no grounding work, no change."""
        rule = _rule(parameters={}, confidence=0.77)
        grounded = _ground_parameters(rule, _facts(), "any text")
        assert grounded.parameters == {}
        assert grounded.confidence == 0.77


class TestEdgeCases:
    """Boundary conditions."""

    def test_value_in_facts_but_not_text_still_grounded(self):
        """If the deterministic extractor found a measurement, trust it even
        if the numeric scan of text missed it (e.g., unicode artifacts)."""
        rule = _rule(parameters={"measurement_mm": 600}, confidence=0.85)
        facts = _facts(measurements=[{"value": "600", "unit": "mm"}])
        source = "Width: 600mm"  # no space — regex still catches it
        grounded = _ground_parameters(rule, facts, source)
        assert grounded.parameters == {"measurement_mm": 600}

    def test_range_value_both_endpoints_grounded(self):
        """If the LLM emits one value from a range, it must be grounded."""
        rule = _rule(parameters={"height_m": 4}, confidence=0.91)
        source = "The ceiling height is between 3 m and 4 m."
        grounded = _ground_parameters(rule, _facts(), source)
        assert grounded.parameters == {"height_m": 4}


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
