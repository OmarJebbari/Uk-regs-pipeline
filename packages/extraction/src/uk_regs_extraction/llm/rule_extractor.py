"""
LLM rule extraction with strict structured outputs.

The LLM's job: interpret prose into a typed ExtractedRule, *anchored* by the
deterministic facts (dimensions/standards/sections) we already pulled.
It must NOT invent values; it must ground every field in the source text.
"""

from __future__ import annotations

import contextlib
import logging
import re
from datetime import date

from pydantic import BaseModel, Field
from uk_regs_core import Category, Citation, Rule, RuleStatus
from uk_regs_core.ids import now_utc, rule_id

from uk_regs_extraction.deterministic.extractors import DeterministicFacts
from uk_regs_extraction.llm.client import LLMClient

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a UK Building Regulations extraction engine.
Your output MUST be grounded in the provided source text — never invent facts.

Rules:
1. Classify each block as one of: regulation, guidance, example, definition, reference.
   Only extract 'regulation' blocks as rules; skip the others.
2. Every numeric value you emit MUST appear verbatim in the deterministic_facts.
   Do NOT convert units or round.
3. Every standards reference MUST come from deterministic_facts.standards.
4. Choose the single best Category for the rule.
5. section_path must match a section token from deterministic_facts.sections when possible.
6. normalized_form is a canonical, lowercase, whitespace-collapsed restatement.
7. If the block is not actually a regulation, set is_regulation=false and leave the rest empty.
"""


class ExtractedRule(BaseModel):
    """Structured output schema for one block."""

    is_regulation: bool = Field(description="False if the block is guidance/example/etc.")
    category: Category = Category.OTHER
    title: str | None = None
    text: str = Field(description="Verbatim or lightly cleaned rule text")
    normalized_form: str
    section_path: str | None = None
    parameters: dict[str, float | str] = Field(default_factory=dict)
    references: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(description="Why this classification + confidence")


class RuleExtractor:
    """Wraps the LLM to turn a ContentBlock into an ExtractedRule."""

    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm

    def extract(
        self,
        *,
        text: str,
        facts: DeterministicFacts,
        doc_id: str,
        version: str,
        page: int,
    ) -> ExtractedRule | None:
        user = (
            f"Source text (page {page}):\n```\n{text}\n```\n\n"
            f"Deterministic facts (must be used as anchors):\n{facts.model_dump() if hasattr(facts, 'model_dump') else _facts_dict(facts)}\n"
        )
        result = self._llm.complete(
            system=SYSTEM_PROMPT,
            user=user,
            response_model=ExtractedRule,
        )
        if not result.is_regulation:
            return None
        # Cross-check: no invented standards
        result.references = [r for r in result.references if _ref_ok(r, facts.standards)]
        # Cross-check: no invented numeric parameters, and penalize confidence
        # for any value the LLM emitted that is not grounded in the source text.
        result = _ground_parameters(result, facts, text)
        return result


def to_rule(
    extracted: ExtractedRule,
    *,
    doc_id: str,
    version: str,
    page: int,
    raw_text: str,
    section_path: str,
    valid_from: date,
    index: int = 0,
) -> Rule:
    """Convert an LLM ExtractedRule into the canonical Rule model."""
    return Rule(
        rule_id=rule_id(doc_id, section_path, index=index),
        doc_id=doc_id,
        version=version,
        section_path=section_path,
        category=extracted.category,
        title=extracted.title,
        text=extracted.text,
        normalized_form=extracted.normalized_form,
        parameters=extracted.parameters,
        references=extracted.references,
        valid_from=valid_from,
        valid_to=None,
        recorded_at=now_utc(),
        status=RuleStatus.PROPOSED,
        citation=Citation(
            doc_id=doc_id,
            version=version,
            page=page,
            raw_text=raw_text,
        ),
        confidence=extracted.confidence,
        extraction_method="llm",
    )


def _facts_dict(f: DeterministicFacts) -> dict:
    return {
        "measurements": f.measurements,
        "standards": f.standards,
        "sections": f.sections,
    }


def _ref_ok(ref: str, allowed: list[str]) -> bool:
    # tolerate minor formatting differences
    norm = ref.replace(" ", "").upper()
    return any(norm == a.replace(" ", "").upper() for a in allowed)


# --- Numeric grounding -------------------------------------------------------
# Any number token (incl. thousands separators, decimals, ranges) that appears
# in the source text. Used to validate LLM-emitted numeric parameter values.
_NUMBER_IN_TEXT = re.compile(r"\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?")


def _ground_parameters(
    result: ExtractedRule,
    facts: DeterministicFacts,
    source_text: str,
) -> ExtractedRule:
    """
    Validate LLM-emitted numeric parameters against the source text.

    For each numeric value in ``result.parameters``, check it appears (as a
    number) somewhere in the source text. If not, treat it as a likely
    hallucination: drop it and apply a confidence penalty so the rule is less
    likely to clear the activation threshold.

    We build the "allowed" set from two sources:
      1. The deterministic measurements (high precision — same unit+value).
      2. A numeric scan of the raw source text (broader — catches "30 minutes",
         "3 storeys", etc., which the deterministic extractor intentionally
         does not capture because they are not physical units).

    Returns the (possibly modified) ExtractedRule.
    """
    if not result.parameters:
        return result

    # Build the allowlist of every numeric token in the source text, compared
    # as floats so that "30" (text) and 30.0 (LLM float) match correctly.
    allowed_values: set[float] = set()
    with contextlib.suppress(ValueError):
        for m in facts.measurements:
            allowed_values.add(float(str(m["value"]).replace(",", "")))
    with contextlib.suppress(ValueError):
        for token in _NUMBER_IN_TEXT.finditer(source_text):
            allowed_values.add(float(token.group(0).replace(",", "")))

    grounded: dict[str, float | str] = {}
    ungrounded_count = 0
    for key, value in result.parameters.items():
        # Non-numeric parameter values (strings like "FD30") are left as-is;
        # the LLM may legitimately encode a qualitative classification.
        if isinstance(value, str) and not _NUMBER_IN_TEXT.search(value):
            grounded[key] = value
            continue
        # For numeric values, parse to float and check membership in the
        # allowlist. Comparing as floats avoids "30" != "30.0" mismatches.
        try:
            v_float = float(str(value).replace(",", ""))
        except ValueError:
            # Can't parse — treat as qualitative, keep it.
            grounded[key] = value
            continue
        if v_float in allowed_values:
            grounded[key] = value
        else:
            ungrounded_count += 1
            logger.warning(
                "Dropping ungrounded parameter %s=%r (not found in source text)",
                key,
                value,
            )

    result.parameters = grounded
    if ungrounded_count > 0:
        # Each hallucinated value reduces confidence. The penalty is linear so
        # multiple violations compound, but never drops below 0.0. This steers
        # the rule toward the review queue rather than auto-activation.
        # We round to 4 decimals to avoid floating-point artefacts downstream.
        penalty = round(0.15 * ungrounded_count, 4)
        result.confidence = round(max(0.0, result.confidence - penalty), 4)
        logger.info(
            "Applied grounding penalty of %.2f (%d ungrounded value(s)); confidence now %.3f",
            penalty,
            ungrounded_count,
            result.confidence,
        )
    return result
