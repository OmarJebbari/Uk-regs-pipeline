"""
The reconciliation engine.

For each new rule:
  1. Gather candidates via matchers (deterministic + semantic).
  2. Decide: MATCH | PARTIAL | NOVEL | CONFLICT, deterministically when possible,
     via LLM only for genuine ambiguity.
  3. Emit a RuleDelta. Low-confidence -> needs_review=True.

Design goal: precision. Err on the side of CONFLICT (human review) rather than
silently superseding a rule.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from uk_regs_core import Decision, Rule, RuleDelta
from uk_regs_core.ids import change_set_id
from uk_regs_extraction.llm.client import LLMClient

from uk_regs_reconcile.matchers import EmbeddingMatcher, Matcher, RuleMatcher, text_similarity
from uk_regs_reconcile.models import Candidate, ReconcileResult, to_delta

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ReconcilerConfig:
    """Thresholds tuned for high precision in a compliance-critical domain."""

    # Minimum candidate score to consider a rule "related" at all. Below this
    # the candidate is treated as noise and the rule is NOVEL.
    candidate_floor: float = 0.30
    # Text similarity at/above which two related rules are the SAME rule (MATCH).
    # 0.92 ≈ "only whitespace/punctuation differs".
    text_match_threshold: float = 0.92
    # Text similarity at/above which two related rules are an AMENDMENT (PARTIAL).
    # Between candidate_floor and text_match_threshold -> PARTIAL.
    text_partial_threshold: float = 0.55
    # Overall confidence below which we route to human review
    review_confidence: float = 0.75
    # Use the LLM only when deterministic decision confidence is below this
    llm_confidence_floor: float = 0.65
    top_k_candidates: int = 8


@dataclass
class Reconciler:
    """
    Compares new rules against the existing corpus and emits a ChangeSet.

    Usage:
        rec = Reconciler(llm=client)
        change_set = rec.reconcile(new_rules, corpus)
    """

    llm: LLMClient | None = None
    config: ReconcilerConfig = field(default_factory=ReconcilerConfig)
    matchers: list[Matcher] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.matchers:
            # Deterministic first (cheaper, higher precision); then semantic.
            self.matchers = [
                RuleMatcher(top_k=self.config.top_k_candidates),
                EmbeddingMatcher(top_k=self.config.top_k_candidates),
            ]

    # ------------------------------------------------------------------ #
    #  Public API
    # ------------------------------------------------------------------ #
    def reconcile(
        self,
        new_rules: list[Rule],
        corpus: list[Rule],
        *,
        doc_id: str,
    ) -> tuple[list[ReconcileResult], str]:
        """
        Reconcile a batch of new rules against the corpus.

        Returns (results, change_set_id).
        """
        cs_id = change_set_id(doc_id)
        results: list[ReconcileResult] = []

        for rule in new_rules:
            try:
                candidates = self._gather_candidates(rule, corpus)
                result = self._decide(rule, candidates)
            except Exception:  # never let one bad rule kill the batch
                logger.exception("reconcile failed for rule %s", rule.rule_id)
                delta = to_delta(
                    new_rule_id=rule.rule_id,
                    decision=Decision.CONFLICT,
                    confidence=0.0,
                    rationale="Reconciliation error; routed to manual review.",
                    diff_summary="ERROR",
                )
                result = ReconcileResult(delta=delta, decided_via="fallback", needs_review=True)
            results.append(result)

        logger.info(
            "reconciled %d rules: %s",
            len(results),
            {d.delta.decision.value: 1 for d in results},  # quick counter
        )
        return results, cs_id

    def to_change_set_deltas(self, results: list[ReconcileResult]) -> list[RuleDelta]:
        return [r.delta for r in results]

    # ------------------------------------------------------------------ #
    #  Candidate gathering
    # ------------------------------------------------------------------ #
    def _gather_candidates(self, new_rule: Rule, corpus: list[Rule]) -> list[Candidate]:
        """Run all matchers and merge/dedupe by rule_id, keeping best score."""
        merged: dict[str, Candidate] = {}
        for matcher in self.matchers:
            try:
                for cand in matcher.find_candidates(new_rule, corpus):
                    existing = merged.get(cand.rule.rule_id)
                    if existing is None or cand.score > existing.score:
                        merged[cand.rule.rule_id] = cand
            except Exception:
                logger.warning("matcher %s failed", type(matcher).__name__, exc_info=True)
        return sorted(merged.values(), key=lambda c: c.score, reverse=True)

    # ------------------------------------------------------------------ #
    #  Decision logic
    # ------------------------------------------------------------------ #
    def _decide(self, new_rule: Rule, candidates: list[Candidate]) -> ReconcileResult:
        # No candidates at all -> novel rule.
        if not candidates:
            return ReconcileResult(
                delta=to_delta(
                    new_rule_id=new_rule.rule_id,
                    decision=Decision.NOVEL,
                    confidence=0.9,
                    rationale="No matching rule found in the existing corpus.",
                    diff_summary="New rule added.",
                ),
                decided_via="deterministic",
            )

        best = candidates[0]
        old = best.rule

        # Strong deterministic check first
        det = self._deterministic_decision(new_rule, old, best.score)
        if det is not None and det.delta.confidence >= self.config.llm_confidence_floor:
            return det

        # If we have an LLM, ask it to adjudicate the close call
        if self.llm is not None and det is not None:
            llm_result = self._llm_decide(new_rule, candidates[:3])
            if llm_result is not None:
                return llm_result

        # Fallback: use the deterministic result (even if low-confidence) or
        # escalate to review.
        if det is not None:
            det.needs_review = det.delta.confidence < self.config.review_confidence
            return det

        # Last resort
        return ReconcileResult(
            delta=to_delta(
                new_rule_id=new_rule.rule_id,
                matched_rule_id=old.rule_id,
                decision=Decision.CONFLICT,
                confidence=0.3,
                rationale="Could not decide automatically; potential overlap with existing rule.",
                diff_summary=f"Possible overlap with {old.rule_id}",
            ),
            decided_via="fallback",
            needs_review=True,
        )

    def _deterministic_decision(
        self, new_rule: Rule, old: Rule, candidate_score: float
    ) -> ReconcileResult | None:
        """
        Try to decide without an LLM. Returns None if it cannot decide confidently.

        Decision model (calibrated for precision):
          - candidate score is only a *plausibility gate* (is this rule related?);
          - the actual MATCH vs PARTIAL decision is driven by text similarity and
            parameter equality, which are the true semantic signals. This keeps the
            engine correct even when only the deterministic RuleMatcher runs (which
            caps candidate scores well below 1.0).
        """
        # Candidate too weak to be related -> caller treats as novel.
        if candidate_score < self.config.candidate_floor:
            return None

        text_sim = text_similarity(new_rule.normalized_form, old.normalized_form)
        params_match = new_rule.parameters == old.parameters

        # Near-identical text: MATCH if params also agree, else PARTIAL (amended).
        if text_sim >= self.config.text_match_threshold:
            if params_match:
                return ReconcileResult(
                    delta=to_delta(
                        new_rule_id=new_rule.rule_id,
                        matched_rule_id=old.rule_id,
                        decision=Decision.MATCH,
                        confidence=min(1.0, 0.5 + 0.5 * text_sim),
                        rationale="Near-identical text and parameters.",
                        diff_summary="Unchanged.",
                    ),
                    decided_via="deterministic",
                )
            diff = self._param_diff(new_rule, old)
            return ReconcileResult(
                delta=to_delta(
                    new_rule_id=new_rule.rule_id,
                    matched_rule_id=old.rule_id,
                    decision=Decision.PARTIAL,
                    confidence=min(0.95, 0.5 + 0.4 * text_sim),
                    rationale="Near-identical text but parameters changed.",
                    diff_summary=diff,
                ),
                decided_via="deterministic",
            )

        # Related rule whose text has drifted -> amendment (PARTIAL).
        if text_sim >= self.config.text_partial_threshold:
            diff = self._param_diff(new_rule, old)
            return ReconcileResult(
                delta=to_delta(
                    new_rule_id=new_rule.rule_id,
                    matched_rule_id=old.rule_id,
                    decision=Decision.PARTIAL,
                    confidence=min(0.9, 0.4 * candidate_score + 0.4 * text_sim),
                    rationale="Related rule with changed text or parameters.",
                    diff_summary=diff,
                ),
                decided_via="deterministic",
            )

        # Could not decide deterministically
        return None

    def _llm_decide(self, new_rule: Rule, candidates: list[Candidate]) -> ReconcileResult | None:
        """Ask the LLM to classify the relationship. Structured output for safety."""
        from pydantic import BaseModel, Field
        from uk_regs_core import Decision as CoreDecision

        class LLMVerdict(BaseModel):
            decision: CoreDecision
            confidence: float = Field(ge=0.0, le=1.0)
            matched_rule_id: str | None = None
            rationale: str
            diff_summary: str = ""

        cand_text = "\n\n".join(
            f"[{i}] rule_id={c.rule.rule_id} (score={c.score:.2f})\n"
            f"section={c.rule.section_path}\n{text_preview(c.rule.text)}"
            for i, c in enumerate(candidates)
        )

        system = (
            "You are a UK Building Regulations reconciliation engine. "
            "Given a NEW rule and up to 3 candidate EXISTING rules, decide the relationship. "
            "Decisions: match (same rule), partial (amended/superseded), novel (unrelated), "
            "conflict (contradictory). Be conservative: if uncertain, choose 'conflict' so a "
            "human reviews it. Never invent a matched_rule_id outside the candidates."
        )
        user = (
            f"NEW RULE:\nrule_id={new_rule.rule_id}\nsection={new_rule.section_path}\n"
            f"{text_preview(new_rule.text)}\n\n"
            f"CANDIDATES:\n{cand_text}"
        )
        try:
            verdict = self.llm.complete(  # type: ignore[union-attr]
                system=system,
                user=user,
                response_model=LLMVerdict,
                temperature=0.0,
            )
        except Exception:
            logger.warning("LLM decide failed", exc_info=True)
            return None

        matched_id = verdict.matched_rule_id
        if matched_id and not any(c.rule.rule_id == matched_id for c in candidates):
            matched_id = candidates[0].rule.rule_id if candidates else None

        return ReconcileResult(
            delta=to_delta(
                new_rule_id=new_rule.rule_id,
                matched_rule_id=matched_id,
                decision=verdict.decision,
                confidence=verdict.confidence,
                rationale=verdict.rationale,
                diff_summary=verdict.diff_summary,
            ),
            decided_via="llm",
            needs_review=verdict.confidence < self.config.review_confidence,
        )

    @staticmethod
    def _param_diff(new_rule: Rule, old: Rule) -> str:
        """Human-readable diff of numeric parameters between two rules."""
        new_p = new_rule.parameters
        old_p = old.parameters
        lines: list[str] = []
        for k in sorted(set(new_p) | set(old_p)):
            nv, ov = new_p.get(k), old_p.get(k)
            if nv != ov:
                lines.append(f"{k}: {ov!r} -> {nv!r}")
        return "; ".join(lines) or "text changed; parameters identical"


def text_preview(text: str, n: int = 600) -> str:
    t = text.strip().replace("\n", " ")
    return t[:n] + ("…" if len(t) > n else "")
