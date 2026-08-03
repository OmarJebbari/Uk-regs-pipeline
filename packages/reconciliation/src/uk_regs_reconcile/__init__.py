"""
Reconciliation engine: compares newly-extracted rules against the existing
corpus and produces a ChangeSet of decisions (match | partial | novel | conflict).

This is the heart of the "remembers old + updates with new" behaviour.
"""

from uk_regs_reconcile.engine import Reconciler, ReconcilerConfig
from uk_regs_reconcile.matchers import EmbeddingMatcher, Matcher, RuleMatcher
from uk_regs_reconcile.models import Candidate, ReconcileInput, ReconcileResult

__all__ = [
    "Reconciler",
    "ReconcilerConfig",
    "Matcher",
    "EmbeddingMatcher",
    "RuleMatcher",
    "Candidate",
    "ReconcileInput",
    "ReconcileResult",
]
