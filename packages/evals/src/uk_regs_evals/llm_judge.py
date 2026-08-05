"""
LLM-as-a-judge scoring for generated context quality.

The deterministic golden suites (extraction / reconciliation / retrieval) prove
the *plumbing* is correct. This module evaluates the *output quality* of the
pipeline — when the API/MCP server produces a "context" for the IDE assistant,
is that context:

  * **Faithful** — every claim is grounded in a source rule (no hallucination).
  * **Relevant** — the context actually addresses the user's query.
  * **Correctly cited** — every cited rule_id is one of the expected rules.

Two implementations are provided behind a common ``Judge`` protocol:

  * ``RubricJudge`` — a deterministic, token-overlap-based scorer that needs
    no network and no API key.  This is the default so the suite runs in CI
    and offline, exactly like the other golden suites.
  * ``LLMJudge`` — delegates the faithfulness/relevance scoring to the
    configured ``LLMClient`` (OpenAI / Anthropic / …).  Used when an API key
    is present for a deeper, semantics-aware evaluation.

Both return the same ``JudgeScore`` so the harness and report are agnostic.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Protocol

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
#  Score container
# --------------------------------------------------------------------------- #
@dataclass(slots=True, frozen=True)
class JudgeScore:
    """Per-case quality scores, all in [0, 1]."""

    faithfulness: float  # claims grounded in source rules (no hallucination)
    answer_relevance: float  # context addresses the query
    citation_precision: float  # cited rule_ids are correct
    rationale: str = ""

    @property
    def overall(self) -> float:
        """Weakest-link score — a compliance context fails if *any* dimension is weak."""
        return min(self.faithfulness, self.answer_relevance, self.citation_precision)


@dataclass(slots=True)
class JudgeCase:
    """A single LLM-judge golden case."""

    id: str
    query: str
    source_rules: list[dict]  # ground-truth rules: [{"rule_id": ..., "text": ...}]
    generated_context: str  # the pipeline output to evaluate
    expected_rule_ids: list[str]  # which rule_ids SHOULD appear in the context
    min_faithfulness: float = 0.80
    min_relevance: float = 0.50
    min_citation_precision: float = 1.00


# --------------------------------------------------------------------------- #
#  Judge protocol
# --------------------------------------------------------------------------- #
class Judge(Protocol):
    """Scores a generated context against source rules."""

    def score(self, case: JudgeCase) -> JudgeScore: ...


# --------------------------------------------------------------------------- #
#  Tokenisation helpers (shared by both judges)
# --------------------------------------------------------------------------- #
_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "with",
        "by",
        "from",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "shall",
        "should",
        "may",
        "might",
        "must",
        "can",
        "could",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "as",
        "if",
        "then",
        "than",
        "which",
        "who",
        "whom",
        "whose",
        "what",
        "when",
        "where",
        "why",
        "how",
        "every",
        "any",
        "all",
        "no",
        "not",
        "none",
        "each",
        "such",
        "herein",
        "thereof",
        "hereby",
        "hereunder",
    ]
)

_WORD_RE = re.compile(r"[a-z0-9]+(?:[°²³]+)?", re.IGNORECASE)


def _tokenize(text: str) -> set[str]:
    """Lowercase alphanumeric tokens, minus stopwords. Keeps units like mm, m²."""
    return {tok for tok in _WORD_RE.findall(text.lower()) if tok not in _STOPWORDS and len(tok) > 1}


def _split_sentences(text: str) -> list[str]:
    """Cheap sentence splitter — good enough for rubric scoring."""
    parts = re.split(r"(?<=[.!?])\s+|\n+", text.strip())
    return [p.strip() for p in parts if len(p.strip()) > 10]


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# --------------------------------------------------------------------------- #
#  RubricJudge — deterministic, offline, zero dependencies
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class RubricJudge:
    """
    Deterministic token-overlap scorer.

    * **Faithfulness**: for each sentence in the generated context, compute the
      *containment* — what fraction of the sentence's content tokens are found
      in the union of all source rule texts.  Containment is used instead of
      Jaccard because it correctly rewards faithful paraphrases (Jaccard would
      unfairly penalise a reworded sentence that conveys the same rule).
    * **Answer relevance**: token containment between the query and the context.
    * **Citation precision**: fraction of cited rule_ids that are expected.
    """

    faithfulness_floor: float = 0.30  # min containment for a "supported" sentence

    def score(self, case: JudgeCase) -> JudgeScore:
        # -- Citation precision -------------------------------------------------
        cited = _extract_cited_ids(case.generated_context)
        if cited:
            correct = [c for c in cited if c in case.expected_rule_ids]
            citation_precision = len(correct) / len(cited)
        else:
            # No citations at all → fail unless the expected set is also empty.
            citation_precision = 1.0 if not case.expected_rule_ids else 0.0

        # -- Faithfulness (containment-based) -----------------------------------
        # Build the union of all source tokens once, then for each generated
        # sentence compute what fraction of its content tokens are supported.
        source_union: set[str] = set()
        for r in case.source_rules:
            source_union |= _tokenize(r.get("text", ""))
        sentences = _split_sentences(case.generated_context)
        if sentences and source_union:
            scores = []
            for sent in sentences:
                stoks = _tokenize(sent)
                if not stoks:
                    continue
                supported = len(stoks & source_union) / len(stoks)
                # Squash gently: ramp from the floor to 1.0.
                if supported >= self.faithfulness_floor:
                    ramp = (supported - self.faithfulness_floor) / (1.0 - self.faithfulness_floor)
                    scores.append(min(1.0, 0.75 + ramp * 0.25))  # 0.75 → 1.0
                else:
                    # Below the floor — likely hallucinated.
                    scores.append(supported / self.faithfulness_floor * 0.5)
            faithfulness = sum(scores) / len(scores) if scores else 0.0
        else:
            faithfulness = 1.0 if not sentences else 0.0

        # -- Answer relevance (containment + jaccard) ---------------------------
        q_tokens = _tokenize(case.query)
        ctx_tokens = _tokenize(case.generated_context)
        if q_tokens:
            containment = len(q_tokens & ctx_tokens) / len(q_tokens)
            jaccard = _jaccard(q_tokens, ctx_tokens)
            relevance = max(jaccard, min(1.0, containment * 1.5))
        else:
            relevance = 0.0

        rationale = (
            f"faith={faithfulness:.2f} (sentences={len(sentences)}), "
            f"rel={relevance:.2f}, "
            f"cite_prec={citation_precision:.2f} (cited={cited})"
        )
        return JudgeScore(
            faithfulness=faithfulness,
            answer_relevance=relevance,
            citation_precision=citation_precision,
            rationale=rationale,
        )


# --------------------------------------------------------------------------- #
#  LLMJudge — uses the configured LLM client for semantic scoring
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class LLMJudge:
    """
    Semantic LLM-as-a-judge.

    Uses the pipeline's own ``LLMClient`` to score faithfulness and relevance
    on a 0–5 scale, then normalises to [0, 1].  Citation precision is still
    computed deterministically (it's an exact-match metric, not a judgment call).

    The LLM is prompted with the source rules and the generated context, and
    asked to identify unsupported claims (hallucinations) and off-topic content.
    """

    client: object  # LLMClient, typed as object to avoid an import cycle
    citation_judge: RubricJudge = field(default_factory=RubricJudge)

    def score(self, case: JudgeCase) -> JudgeScore:
        from pydantic import BaseModel, Field

        class _LLMScore(BaseModel):
            faithfulness: float = Field(ge=0, le=5, description="5 = every claim grounded")
            relevance: float = Field(ge=0, le=5, description="5 = perfectly on-topic")
            issues: list[str] = Field(default_factory=list)

        rules_block = "\n".join(
            f"- [{r.get('rule_id', '?')}] {r.get('text', '')}" for r in case.source_rules
        )
        system = (
            "You are a strict compliance auditor. Score the generated context against "
            "the source rules. Faithfulness: are ALL claims supported by the source? "
            "Relevance: does it address the query? Be harsh — hallucinated dimensions "
            "or standards are faithfulness=0."
        )
        user = (
            f"## Query\n{case.query}\n\n"
            f"## Source rules (ground truth)\n{rules_block}\n\n"
            f"## Generated context to evaluate\n{case.generated_context}\n\n"
            "Return JSON: faithfulness (0-5), relevance (0-5), issues (list of strings)."
        )

        try:
            result: _LLMScore = self.client.complete(
                system=system, user=user, response_model=_LLMScore, temperature=0.0
            )
            faith = result.faithfulness / 5.0
            rel = result.relevance / 5.0
        except Exception:
            logger.exception("LLM judge failed for %s — falling back to rubric", case.id)
            rb = self.citation_judge.score(case)
            return rb

        # Citation precision is deterministic.
        cite_prec = self.citation_judge.score(case).citation_precision
        return JudgeScore(
            faithfulness=faith,
            answer_relevance=rel,
            citation_precision=cite_prec,
            rationale=f"LLM scores: faith={faith:.2f}, rel={rel:.2f}; issues={result.issues}",
        )


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #
# Require the dash/underscore separator so tokens like "FD60S" (a fire-door
# rating) are NOT mistaken for rule citations like "FIR-301".
_CITATION_RE = re.compile(r"\b([A-Z]{2,4}[-_]\d{2,4}[A-Z]?)\b")


def _extract_cited_ids(text: str) -> list[str]:
    """
    Extract rule_id-like tokens from generated context.

    Looks for patterns like ``FIRE-001``, ``STR-102``, ``rule:adb-001`` etc.
    A separator (dash or underscore) between the alpha prefix and numeric body
    is required so that inline ratings like ``FD60S`` are not misclassified.
    """
    return _CITATION_RE.findall(text)


def judge_factory(*, llm_client: object | None = None) -> Judge:
    """
    Return the best available judge.

    * If an ``llm_client`` is provided (or one can be built from settings),
      use ``LLMJudge`` for semantic scoring.
    * Otherwise fall back to ``RubricJudge`` (deterministic, offline).
    """
    if llm_client is not None:
        return LLMJudge(client=llm_client)
    try:
        from uk_regs_extraction.llm.client import get_llm_client

        client = get_llm_client()
        return LLMJudge(client=client)
    except Exception:
        logger.info("No LLM client available — using RubricJudge (deterministic)")
        return RubricJudge()
