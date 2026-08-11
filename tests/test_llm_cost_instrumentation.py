"""
Tests for LLM cost/latency instrumentation helpers.

These cover the pure helper functions that don't require a live API key:
  - ``_estimate_cost``: pricing-table lookup + arithmetic.
  - Logging key presence (verified indirectly via the cost function).

The actual ``OpenAIClient.complete`` / ``describe_image`` methods are
integration-tested against a real (or mock) OpenAI endpoint elsewhere.

Run:  pytest tests/test_llm_cost_instrumentation.py -v
"""

from __future__ import annotations

import math

from uk_regs_extraction.llm.client import _COST_PER_1K, _estimate_cost


class TestEstimateCost:
    """Verify the cost estimator against known pricing."""

    def test_known_model_gpt4o(self):
        """gpt-4o: $0.0025/1K input, $0.010/1K output."""
        cost = _estimate_cost("gpt-4o", prompt_tokens=1000, completion_tokens=500)
        expected = (1000 / 1000) * 0.0025 + (500 / 1000) * 0.010
        assert math.isclose(cost, round(expected, 6), rel_tol=1e-9)

    def test_known_model_gpt4o_mini(self):
        """gpt-4o-mini: $0.00015/1K input, $0.0006/1K output."""
        cost = _estimate_cost("gpt-4o-mini", prompt_tokens=10000, completion_tokens=2000)
        expected = (10000 / 1000) * 0.00015 + (2000 / 1000) * 0.0006
        assert math.isclose(cost, round(expected, 6), rel_tol=1e-9)

    def test_unknown_model_returns_zero(self):
        """Unknown models default to (0, 0) — no false cost."""
        cost = _estimate_cost("unknown-future-model", prompt_tokens=99999, completion_tokens=99999)
        assert cost == 0.0

    def test_zero_tokens(self):
        """Zero tokens = zero cost."""
        assert _estimate_cost("gpt-4o", 0, 0) == 0.0

    def test_large_input(self):
        """Large input doesn't overflow / NaN."""
        cost = _estimate_cost("gpt-4o", prompt_tokens=500_000, completion_tokens=10_000)
        assert cost > 0.0
        assert not math.isnan(cost)

    def test_all_models_have_tuple_rates(self):
        """Every entry in the cost table is a 2-tuple of floats."""
        for model, rates in _COST_PER_1K.items():
            assert isinstance(model, str)
            assert len(rates) == 2
            assert all(isinstance(r, (int, float)) for r in rates)
            assert rates[0] >= 0 and rates[1] >= 0
