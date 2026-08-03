"""
Internal data structures for reconciliation.

These are engine-facing; the public contract with the rest of the system is
expressed via core.models (Rule, RuleDelta, ChangeSet).
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field
from uk_regs_core import Decision, Rule, RuleDelta


class Candidate(BaseModel):
    """An existing rule returned by a Matcher as a potential match."""

    model_config = ConfigDict(frozen=True)

    rule: Rule
    score: float = Field(ge=0.0, le=1.0)
    matched_on: str = Field(
        description="What signalled the match, e.g. 'embedding', 'section_path'"
    )


class ReconcileInput(BaseModel):
    """One new rule plus its candidate matches, ready for the decider."""

    new_rule: Rule
    candidates: list[Candidate] = Field(default_factory=list)


class ReconcileResult(BaseModel):
    """The engine's verdict for one new rule."""

    delta: RuleDelta
    decided_via: str = Field(description="'deterministic' | 'llm' | 'fallback'")
    needs_review: bool = False


def to_delta(
    *,
    new_rule_id: str,
    decision: Decision,
    confidence: float,
    rationale: str,
    diff_summary: str,
    matched_rule_id: str | None = None,
    valid_to_override: date | None = None,
) -> RuleDelta:
    """Construct a RuleDelta with optional matched-rule linkage."""
    return RuleDelta(
        new_rule_id=new_rule_id,
        matched_rule_id=matched_rule_id,
        decision=decision,
        confidence=confidence,
        rationale=rationale,
        diff_summary=diff_summary,
    )
