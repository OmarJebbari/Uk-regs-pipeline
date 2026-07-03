"""
LLM rule extraction with strict structured outputs.

The LLM's job: interpret prose into a typed ExtractedRule, *anchored* by the
deterministic facts (dimensions/standards/sections) we already pulled.
It must NOT invent values; it must ground every field in the source text.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from uk_regs_core import Category, Citation, Rule, RuleStatus
from uk_regs_core.ids import now_utc, rule_id

from uk_regs_extraction.deterministic.extractors import DeterministicFacts
from uk_regs_extraction.llm.client import LLMClient


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