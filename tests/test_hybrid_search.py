"""
Unit tests for the HybridSearcher (Reciprocal Rank Fusion).

These tests are pure — no database, no embedder, no network. They verify
the fusion math and the three modes (hybrid / semantic / keyword) using
synthetic callables that return canned rankings.

Run:  pytest tests/test_hybrid_search.py -v
"""

from __future__ import annotations

import pytest
from uk_regs_api.service import HybridSearcher


# --------------------------------------------------------------------------- #
#  Synthetic rule fixtures (lightweight — only rule_id + text matter here)
# --------------------------------------------------------------------------- #
class FakeRule:
    """Minimal stand-in for Rule; HybridSearcher only needs .rule_id."""

    def __init__(self, rule_id: str) -> None:
        self.rule_id = rule_id
        self.text = rule_id  # for debugging


def _make_rules(*ids: str) -> list[FakeRule]:
    return [FakeRule(rid) for rid in ids]


# --------------------------------------------------------------------------- #
#  Helpers to build canned semantic/keyword results
#  Each fn signature matches what HybridSearcher expects:
#    semantic_fn(query_vec, top_k=k) -> list[tuple[Rule, float]]
#    keyword_fn(query, top_k=k)      -> list[tuple[Rule, float]]
# --------------------------------------------------------------------------- #
def make_semantic_fn(ranked_ids: list[str]):
    """Returns a semantic leg that always returns these ids (score=0.9 descending)."""
    base = 0.9
    def fn(query_vec, top_k=10):
        rules = _make_rules(*ranked_ids[:top_k])
        scores = [round(base - i * 0.05, 3) for i in range(len(rules))]
        return list(zip(rules, scores, strict=False))
    return fn


def make_keyword_fn(ranked_ids: list[str]):
    """Returns a keyword leg that always returns these ids (score by ts_rank)."""
    def fn(query, top_k=10):
        rules = _make_rules(*ranked_ids[:top_k])
        scores = [round(1.0 - i * 0.1, 3) for i in range(len(rules))]
        return list(zip(rules, scores, strict=False))
    return fn


# --------------------------------------------------------------------------- #
#  Mode: keyword
# --------------------------------------------------------------------------- #
class TestKeywordMode:
    def test_returns_keyword_results_only(self):
        sem = make_semantic_fn(["a", "b", "c"])
        kw = make_keyword_fn(["x", "y", "z"])
        s = HybridSearcher(semantic_fn=sem, keyword_fn=kw)
        hits = s.search("query", query_vec=[0.1], top_k=5, mode="keyword")
        ids = [h.rule.rule_id for h in hits]
        assert ids == ["x", "y", "z"]

    def test_keyword_mode_ignores_query_vec(self):
        sem = make_semantic_fn(["a", "b"])
        kw = make_keyword_fn(["p", "q"])
        s = HybridSearcher(semantic_fn=sem, keyword_fn=kw)
        # query_vec is None — keyword mode should still work
        hits = s.search("query", query_vec=None, top_k=5, mode="keyword")
        assert [h.rule.rule_id for h in hits] == ["p", "q"]


# --------------------------------------------------------------------------- #
#  Mode: semantic
# --------------------------------------------------------------------------- #
class TestSemanticMode:
    def test_returns_semantic_results_only(self):
        sem = make_semantic_fn(["a", "b", "c"])
        kw = make_keyword_fn(["x", "y", "z"])
        s = HybridSearcher(semantic_fn=sem, keyword_fn=kw)
        hits = s.search("query", query_vec=[0.1], top_k=5, mode="semantic")
        ids = [h.rule.rule_id for h in hits]
        assert ids == ["a", "b", "c"]

    def test_semantic_mode_returns_empty_if_no_query_vec(self):
        sem = make_semantic_fn(["a", "b"])
        kw = make_keyword_fn(["x", "y"])
        s = HybridSearcher(semantic_fn=sem, keyword_fn=kw)
        hits = s.search("query", query_vec=None, top_k=5, mode="semantic")
        assert hits == []


# --------------------------------------------------------------------------- #
#  Mode: hybrid (RRF)
# --------------------------------------------------------------------------- #
class TestHybridMode:
    def test_union_of_both_legs(self):
        """RRF must include docs that appear in EITHER leg."""
        sem = make_semantic_fn(["a", "b", "c"])
        kw = make_keyword_fn(["c", "d", "e"])
        s = HybridSearcher(semantic_fn=sem, keyword_fn=kw, rrf_k=1)
        hits = s.search("q", query_vec=[0.1], top_k=10, mode="hybrid")
        ids = {h.rule.rule_id for h in hits}
        assert ids == {"a", "b", "c", "d", "e"}

    def test_doc_in_both_legs_ranks_higher(self):
        """A doc present in both legs gets score 1/(k+r1) + 1/(k+r2) > single-leg."""
        sem = make_semantic_fn(["a", "b"])           # a rank 1, b rank 2
        kw = make_keyword_fn(["b", "c"])             # b rank 1, c rank 2
        s = HybridSearcher(semantic_fn=sem, keyword_fn=kw, rrf_k=1)
        hits = s.search("q", query_vec=[0.1], top_k=10, mode="hybrid")
        # b is rank-1 in kw + rank-2 in sem = 1/2 + 1/3 = 0.833
        # a is rank-1 in sem only = 1/2 = 0.5
        assert hits[0].rule.rule_id == "b"
        assert hits[0].score > hits[1].score

    def test_scores_are_rrf_formula(self):
        sem = make_semantic_fn(["a"])  # rank 1 in semantic
        kw = make_keyword_fn(["a"])    # rank 1 in keyword
        k = 60
        s = HybridSearcher(semantic_fn=sem, keyword_fn=kw, rrf_k=k)
        hits = s.search("q", query_vec=[0.1], top_k=5, mode="hybrid")
        expected = 1.0 / (k + 1) + 1.0 / (k + 1)
        assert abs(hits[0].score - expected) < 1e-9

    def test_top_k_limit(self):
        sem = make_semantic_fn([f"s{i}" for i in range(20)])
        kw = make_keyword_fn([f"k{i}" for i in range(20)])
        s = HybridSearcher(semantic_fn=sem, keyword_fn=kw)
        hits = s.search("q", query_vec=[0.1], top_k=5, mode="hybrid")
        assert len(hits) == 5

    def test_missing_query_vec_degrades_gracefully(self):
        """If embeddings unavailable (query_vec=None), hybrid still runs keyword leg."""
        sem = make_semantic_fn(["a", "b"])
        kw = make_keyword_fn(["x", "y"])
        s = HybridSearcher(semantic_fn=sem, keyword_fn=kw)
        hits = s.search("q", query_vec=None, top_k=5, mode="hybrid")
        ids = [h.rule.rule_id for h in hits]
        # Only keyword leg fires; "a", "b" should NOT appear
        assert ids == ["x", "y"]

    def test_semantic_exception_does_not_crash(self):
        def bad_sem(query_vec, top_k=10):
            raise RuntimeError("embedder down")
        kw = make_keyword_fn(["x", "y"])
        s = HybridSearcher(semantic_fn=bad_sem, keyword_fn=kw)
        hits = s.search("q", query_vec=[0.1], top_k=5, mode="hybrid")
        assert [h.rule.rule_id for h in hits] == ["x", "y"]

    def test_keyword_exception_does_not_crash(self):
        sem = make_semantic_fn(["a", "b"])
        def bad_kw(query, top_k=10):
            raise RuntimeError("bm25 index missing")
        s = HybridSearcher(semantic_fn=sem, keyword_fn=bad_kw)
        hits = s.search("q", query_vec=[0.1], top_k=5, mode="hybrid")
        assert [h.rule.rule_id for h in hits] == ["a", "b"]


# --------------------------------------------------------------------------- #
#  Candidate pool expansion
# --------------------------------------------------------------------------- #
class TestCandidateExpansion:
    def test_candidate_k_widens_pool(self):
        """Even with top_k=2, the searcher should query legs with candidate_k."""
        called_sem = {}
        called_kw = {}
        sem = make_semantic_fn([f"s{i}" for i in range(50)])
        kw = make_keyword_fn([f"k{i}" for i in range(50)])
        def sem_wrapper(qv, top_k=10):
            called_sem["top_k"] = top_k
            return sem(qv, top_k=top_k)
        def kw_wrapper(q, top_k=10):
            called_kw["top_k"] = top_k
            return kw(q, top_k=top_k)
        s = HybridSearcher(
            semantic_fn=sem_wrapper, keyword_fn=kw_wrapper, candidate_k=30
        )
        s.search("q", query_vec=[0.1], top_k=2, mode="hybrid")
        assert called_sem["top_k"] >= 30
        assert called_kw["top_k"] >= 30


# --------------------------------------------------------------------------- #
#  min_score filter
# --------------------------------------------------------------------------- #
class TestMinScoreFilter:
    def test_min_score_filters_low_confidence(self):
        # Only "a" in both legs → high score; others single-leg → lower.
        sem = make_semantic_fn(["a", "b", "c"])
        kw = make_keyword_fn(["a", "d"])
        s = HybridSearcher(semantic_fn=sem, keyword_fn=kw, rrf_k=60)
        # Get the score of "a" (both legs, rank 1 each)
        all_hits = s.search("q", query_vec=[0.1], top_k=10, mode="hybrid")
        a_score = next(h.score for h in all_hits if h.rule.rule_id == "a")
        # Now set min_score just above the second-place doc
        s2 = HybridSearcher(
            semantic_fn=sem, keyword_fn=kw, rrf_k=60, min_score=a_score
        )
        filtered = s2.search("q", query_vec=[0.1], top_k=10, mode="hybrid")
        filtered_ids = {h.rule.rule_id for h in filtered}
        assert "a" in filtered_ids
        # Single-leg docs should be filtered out
        assert len(filtered_ids) == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])