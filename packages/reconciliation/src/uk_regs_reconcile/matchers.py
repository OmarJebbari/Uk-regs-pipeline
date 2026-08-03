"""
Matchers: find candidate existing rules that a new rule MIGHT relate to.

A matcher returns zero or more Candidates with similarity scores. The engine
then uses a decider to turn the best candidate into a Decision.

Pluggable: EmbeddingMatcher (semantic), RuleMatcher (deterministic keys),
Future: HybridMatcher (RRF combination).
"""

from __future__ import annotations

import abc
import re
from difflib import SequenceMatcher

from uk_regs_core import Rule

from uk_regs_reconcile.models import Candidate


def _section_key(section_path: str) -> str:
    """Normalised section path: 'AD-B/v2/2/2.3/b' -> 'ad-b/2/2.3/b'."""
    # drop the version segment, lowercase, strip empties
    parts = [p for p in re.split(r"[/\\]+", section_path.lower().strip()) if p]
    # remove tokens that look like versions (v1, 2025-ed1, etc.)
    parts = [p for p in parts if not re.match(r"^v\d+(-|$)|^\d{4}-ed", p)]
    return "/".join(parts)


def _params_signature(rule: Rule) -> frozenset[str]:
    """Set of 'key=value' strings for deterministic comparison of constraints."""
    return frozenset(f"{k}={v}" for k, v in sorted(rule.parameters.items()))


def _refs_signature(rule: Rule) -> frozenset[str]:
    return frozenset(r.replace(" ", "").upper() for r in rule.references)


class Matcher(abc.ABC):
    """Base class. Subclasses implement `find_candidates`."""

    top_k: int = 10

    @abc.abstractmethod
    def find_candidates(self, new_rule: Rule, corpus: list[Rule]) -> list[Candidate]: ...


class RuleMatcher(Matcher):
    """
    Deterministic matcher: same section path, same references, or parameter overlap.

    High-precision / low-recall. Used to find strong candidates cheaply before
    (or instead of) invoking embeddings/LLM.
    """

    def __init__(self, top_k: int = 10) -> None:
        self.top_k = top_k

    def find_candidates(self, new_rule: Rule, corpus: list[Rule]) -> list[Candidate]:
        new_sec = _section_key(new_rule.section_path)
        new_refs = _refs_signature(new_rule)
        new_params = _params_signature(new_rule)

        scored: list[Candidate] = []
        for old in corpus:
            if old.status.value not in ("active", "superseded"):
                continue
            score = 0.0
            reasons: list[str] = []

            # Section path exact match is a very strong signal
            if new_sec and _section_key(old.section_path) == new_sec:
                score += 0.6
                reasons.append("section_path")

            # Shared standards references
            shared_refs = new_refs & _refs_signature(old)
            if shared_refs:
                score += min(0.25, 0.05 * len(shared_refs))
                reasons.append("references")

            # Parameter overlap (same numeric constraints)
            shared_params = new_params & _params_signature(old)
            if shared_params:
                score += min(0.15, 0.03 * len(shared_params))
                reasons.append("parameters")

            if score > 0:
                scored.append(
                    Candidate(
                        rule=old,
                        score=min(1.0, score),
                        matched_on="+".join(reasons) or "rule",
                    )
                )

        scored.sort(key=lambda c: c.score, reverse=True)
        return scored[: self.top_k]


class EmbeddingMatcher(Matcher):
    """
    Semantic matcher: cosine similarity over rule embeddings.

    Expects rules to carry an `_embedding` attribute OR uses a caller-supplied
    embedder. Falls back gracefully if embeddings are missing.
    """

    def __init__(self, top_k: int = 10, min_score: float = 0.55) -> None:
        self.top_k = top_k
        self.min_score = min_score

    def find_candidates(self, new_rule: Rule, corpus: list[Rule]) -> list[Candidate]:
        import numpy as np

        new_vec = _get_embedding(new_rule)
        if new_vec is None:
            return []

        scored: list[Candidate] = []
        for old in corpus:
            old_vec = _get_embedding(old)
            if old_vec is None:
                continue
            sim = float(
                np.dot(new_vec, old_vec)
                / (np.linalg.norm(new_vec) * np.linalg.norm(old_vec) + 1e-9)
            )
            if sim >= self.min_score:
                scored.append(Candidate(rule=old, score=sim, matched_on="embedding"))

        scored.sort(key=lambda c: c.score, reverse=True)
        return scored[: self.top_k]


def _get_embedding(rule: Rule) -> list[float] | None:
    """Pull an embedding vector from a rule, if present."""
    # Rules are frozen Pydantic models; embeddings are stored out-of-band by
    # the pipeline in private fields via model_extra, or attached by the caller.
    extra = getattr(rule, "__pydantic_extra__", None) or {}
    return extra.get("_embedding")


def text_similarity(a: str, b: str) -> float:
    """Cheap lexical similarity fallback (ratio of longest common subsequence)."""
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()
