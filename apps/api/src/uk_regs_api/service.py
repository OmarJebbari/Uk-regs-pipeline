"""
Service layer for the context API.

Abstracts the storage backend so the API can run:
  - against a real Postgres + pgvector backend (``RealBackend``)
  - with an in-memory mock (``MockBackend``) for demos / tests / first-run

The FastAPI routes depend only on the ``ContextBackend`` Protocol, so the
backend can be swapped without touching the HTTP layer.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from functools import lru_cache
from typing import Literal, Protocol

from uk_regs_core import (
    Category,
    ChangeSet,
    Citation,
    Figure,
    Rule,
    RuleDelta,
    RuleStatus,
)
from uk_regs_core.enums import Decision

from uk_regs_api.stats import PipelineStats

# Search mode: hybrid (RRF fusion), semantic (vector only), keyword (BM25 only).
SearchMode = Literal["hybrid", "semantic", "keyword"]


# --------------------------------------------------------------------------- #
#  DTOs (API-facing view models)
# --------------------------------------------------------------------------- #
class RuleHit:
    """A search hit: rule + relevance score + citation."""

    def __init__(self, rule: Rule, score: float) -> None:
        self.rule = rule
        self.score = score

    def to_dict(self) -> dict:
        return {
            "score": round(self.score, 4),
            "rule": _rule_to_dict(self.rule),
            "citation": _citation_to_dict(self.rule.citation),
        }


# --------------------------------------------------------------------------- #
#  Backend protocol
# --------------------------------------------------------------------------- #
class ContextBackend(Protocol):
    def search(self, query: str, top_k: int = 10, mode: SearchMode = "hybrid") -> list[RuleHit]: ...

    def get_rule(self, rule_id: str) -> Rule | None: ...

    def get_current(self, topic: str | None = None, category: str | None = None) -> list[Rule]: ...

    def get_figure(self, rule_id: str) -> Figure | None: ...

    def list_changes(self, status: str | None = None, limit: int = 50) -> list[ChangeSet]: ...

    def get_change_set(self, change_set_id: str) -> ChangeSet | None: ...

    def review_change_set(
        self, change_set_id: str, decision: str, reviewer: str, notes: str = ""
    ) -> ChangeSet | None: ...

    def diff(self, rule_id: str) -> dict: ...

    def get_ops_stats(self) -> PipelineStats: ...

    def list_audit_events(
        self,
        *,
        entity: str | None = None,
        entity_id: str | None = None,
        actor: str | None = None,
        action: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]: ...


# --------------------------------------------------------------------------- #
#  Hybrid search — Reciprocal Rank Fusion (Phase 5-2)
# --------------------------------------------------------------------------- #
class HybridSearcher:
    """
    Fuses BM25 (keyword) + vector (semantic) results via Reciprocal Rank
    Fusion (RRF).

    RRF is score-agnostic: it only cares about the *rank* of each result in
    each leg, which sidesteps the incomparable-score problem (BM25 ts_rank_cd
    is unbounded; cosine similarity is in [0, 1]). The fused score for a
    document that appears at rank ``r1`` in the keyword leg and ``r2`` in the
    semantic leg (1-indexed; missing = infinity) is::

        score = 1/(k + r1) + 1/(k + r2)

    where ``k`` is a smoothing constant (default 60, Cormack et al. 2009).

    The searcher is injected with two callables — ``semantic_fn`` and
    ``keyword_fn`` — each returning ``list[tuple[Rule, float]]`` with scores
    already normalised to ``[0, 1]``. This keeps the fusion logic pure and
    trivially unit-testable without a database.
    """

    def __init__(
        self,
        semantic_fn,
        keyword_fn,
        *,
        rrf_k: int = 60,
        candidate_k: int = 50,
        min_score: float = 0.0,
    ) -> None:
        self._semantic_fn = semantic_fn
        self._keyword_fn = keyword_fn
        self.rrf_k = rrf_k
        self.candidate_k = candidate_k
        self.min_score = min_score

    def search(
        self,
        query: str,
        query_vec: list[float] | None,
        *,
        top_k: int = 10,
        mode: SearchMode = "hybrid",
    ) -> list[RuleHit]:
        k = max(top_k, self.candidate_k)

        if mode == "semantic":
            if query_vec is None:
                return []
            rows = self._semantic_fn(query_vec, top_k=k)
            return [RuleHit(r, s) for r, s in rows[:top_k]]

        if mode == "keyword":
            rows = self._keyword_fn(query, top_k=k)
            return [RuleHit(r, s) for r, s in rows[:top_k]]

        # ---- hybrid: RRF fusion ----
        sem_rows: list[tuple[Rule, float]] = []
        if query_vec is not None:
            try:
                sem_rows = self._semantic_fn(query_vec, top_k=k)
            except Exception:
                sem_rows = []

        try:
            kw_rows = self._keyword_fn(query, top_k=k)
        except Exception:
            kw_rows = []

        # Build rank maps (1-indexed).
        sem_rank = {r.rule_id: i + 1 for i, (r, _) in enumerate(sem_rows)}
        kw_rank = {r.rule_id: i + 1 for i, (r, _) in enumerate(kw_rows)}

        # Union of rule_ids, keep a reference rule object per id.
        rule_by_id: dict[str, Rule] = {}
        for r, _ in sem_rows + kw_rows:
            rule_by_id.setdefault(r.rule_id, r)

        fused: list[tuple[Rule, float]] = []
        for rid, rule in rule_by_id.items():
            score = 0.0
            if rid in sem_rank:
                score += 1.0 / (self.rrf_k + sem_rank[rid])
            if rid in kw_rank:
                score += 1.0 / (self.rrf_k + kw_rank[rid])
            if score >= self.min_score:
                fused.append((rule, score))

        fused.sort(key=lambda rs: rs[1], reverse=True)
        return [RuleHit(r, s) for r, s in fused[:top_k]]


# --------------------------------------------------------------------------- #
#  Real backend (Postgres + pgvector + embedder)
# --------------------------------------------------------------------------- #
class RealBackend:
    """Wires repositories + embedder together. Lazy-initialised."""

    def __init__(self) -> None:
        self._rules = None
        self._changes = None
        self._figures = None
        self._embedder = None
        self._graph = None

    # lazy so import of the api package doesn't require a running DB
    def _ensure(self) -> None:
        if self._rules is not None:
            return
        from uk_regs_knowledge.repositories.relational_repo import (
            ChangeSetRepository,
            FigureRepository,
            RuleRepository,
        )

        self._rules = RuleRepository()
        self._changes = ChangeSetRepository()
        self._figures = FigureRepository()

    def _get_embedder(self):
        if self._embedder is None:
            try:
                from uk_regs_knowledge.embeddings import get_embedder

                self._embedder = get_embedder()
            except Exception:
                self._embedder = "unavailable"
        return self._embedder

    def search(self, query: str, top_k: int = 10, mode: SearchMode = "hybrid") -> list[RuleHit]:
        """
        Hybrid search over the rules table.

        ``mode``:
          - ``hybrid`` (default): fuse BM25 + vector via RRF
          - ``semantic``: vector cosine only
          - ``keyword``: BM25 full-text only

        Falls back gracefully: if embeddings are unavailable, every mode
        degrades to keyword (BM25/ILIKE) so search never hard-fails.
        """
        self._ensure()

        from uk_regs_core.config import get_settings

        s = get_settings()

        # Embed the query once (used by hybrid + semantic).
        query_vec: list[float] | None = None
        emb = self._get_embedder()
        if emb and emb != "unavailable":
            try:
                query_vec = emb.embed_text(query)
            except Exception:
                query_vec = None

        # No embeddings → force keyword mode regardless of `mode`.
        if query_vec is None and mode != "keyword":
            effective_mode: SearchMode = "keyword"
        else:
            effective_mode = mode

        searcher = HybridSearcher(
            semantic_fn=self._rules.search_semantic,
            keyword_fn=self._rules.search_bm25,
            rrf_k=s.search_rrf_k,
            candidate_k=s.search_candidate_k,
            min_score=s.search_min_score,
        )
        return searcher.search(query, query_vec, top_k=top_k, mode=effective_mode)

    def get_rule(self, rule_id: str) -> Rule | None:
        self._ensure()
        return self._rules.get_rule_by_id(rule_id)

    def get_current(self, topic: str | None = None, category: str | None = None) -> list[Rule]:
        self._ensure()
        if category:
            return self._rules.get_by_category(category)
        all_rules = self._rules.get_current_rules()
        if topic:
            tl = topic.lower()
            all_rules = [
                r
                for r in all_rules
                if tl in (r.text or "").lower()
                or tl in (r.title or "").lower()
                or tl in (r.normalized_form or "").lower()
            ]
        return all_rules

    def get_figure(self, rule_id: str) -> Figure | None:
        self._ensure()
        try:
            figs = self._figures.list_for_rule(rule_id)
            return figs[0] if figs else None
        except Exception:
            return None

    def list_changes(self, status: str | None = None, limit: int = 50) -> list[ChangeSet]:
        self._ensure()
        return self._changes.list_changes(status=status, limit=limit)

    def get_change_set(self, change_set_id: str) -> ChangeSet | None:
        self._ensure()
        return self._changes.get_by_id(change_set_id)

    def review_change_set(
        self, change_set_id: str, decision: str, reviewer: str, notes: str = ""
    ) -> ChangeSet | None:
        self._ensure()
        return self._changes.review(change_set_id, decision, reviewer, notes)

    def get_ops_stats(self) -> PipelineStats:
        """Aggregate pipeline stats from all repositories for the ops dashboard."""
        self._ensure()
        from uk_regs_knowledge.repositories.relational_repo import DocumentRepository

        docs = DocumentRepository()
        try:
            documents = docs.count_documents()
            versions = docs.count_versions()
        except Exception:
            documents, versions = 0, 0

        rules_by_status = self._rules.count_by_status()
        return PipelineStats(
            active_rules=rules_by_status.get("active", 0),
            total_rules=self._rules.total_count(),
            rules_by_status=rules_by_status,
            rules_by_category=self._rules.count_by_category(),
            documents=documents,
            document_versions=versions,
            change_sets_by_status=self._changes.count_by_status(),
            pending_review=self._changes.count_by_status().get("proposed", 0),
        )

    def list_audit_events(
        self,
        *,
        entity: str | None = None,
        entity_id: str | None = None,
        actor: str | None = None,
        action: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """Query the append-only audit trail (newest-first)."""
        self._ensure()
        from uk_regs_knowledge.repositories.relational_repo import AuditRepository

        repo = AuditRepository()  # uses the global connection pool
        events = repo.list_events(
            entity=entity,
            entity_id=entity_id,
            actor=actor,
            action=action,
            limit=limit,
            offset=offset,
        )
        # Serialise datetime for JSON
        for e in events:
            if hasattr(e.get("at"), "isoformat"):
                e["at"] = e["at"].isoformat()
        return events

    def diff(self, rule_id: str) -> dict:
        self._ensure()
        rule = self._rules.get_rule_by_id(rule_id)
        if not rule:
            return {"error": "not found", "rule_id": rule_id}
        # Try the graph for supersession provenance; fall back to SQL mirror.
        previous: dict | None = None
        try:
            previous = self._diff_from_graph(rule_id)
        except Exception:
            previous = self._diff_from_sql(rule_id)
        return {
            "rule_id": rule_id,
            "current": _rule_to_dict(rule),
            "previous": previous,
        }

    def _diff_from_graph(self, rule_id: str) -> dict | None:
        if self._graph is None:
            try:
                from uk_regs_knowledge.graph.connection import get_driver

                self._graph = get_driver()
            except Exception:
                self._graph = None
                return None
        from uk_regs_knowledge.graph.graph_repo import GraphRepository

        history = GraphRepository(self._graph).version_history(rule_id)
        # history is oldest-first; the predecessor is the entry right before us.
        prev_entries = [h for h in history if h.get("rule_id") != rule_id]
        if not prev_entries:
            return None
        prev = prev_entries[-1]
        return {
            "rule_id": prev.get("rule_id"),
            "version": prev.get("version"),
            "text": prev.get("text"),
            "valid_from": prev.get("valid_from"),
            "valid_to": prev.get("valid_to"),
            "source": "graph",
        }

    def _diff_from_sql(self, rule_id: str) -> dict | None:
        """Fallback: read the SQL ``supersessions`` mirror table.

        Uses psycopg 3's ``pool.connection()`` context manager, which checks
        out a connection, commits on a clean exit, rolls back on error, and
        returns the connection to the pool automatically.
        """
        from uk_regs_knowledge.relational.connection import get_pool

        pool = get_pool()
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT old_rule_id, reason FROM supersessions WHERE new_rule_id = %s",
                (rule_id,),
            )
            row = cur.fetchone()
        if not row:
            return None
        old_id, reason = row
        # Fetch the previous rule via the repository (uses its own pooled conn).
        prev_rule = self._rules.get_rule_by_id(old_id)
        if not prev_rule:
            return {"rule_id": old_id, "reason": reason, "source": "sql"}
        return {
            "rule_id": old_id,
            "reason": reason,
            "text": prev_rule.text,
            "parameters": prev_rule.parameters,
            "source": "sql",
        }


# --------------------------------------------------------------------------- #
#  Mock backend — always works, no DB. Great for demos / first run.
# --------------------------------------------------------------------------- #
def _mock_rules() -> list[Rule]:
    now = datetime.now(UTC)
    today = date.today()
    cite = lambda p, t: Citation(  # noqa: E731
        doc_id="ad-b-2026", version="2026-ed1", page=p, raw_text=t, url=None
    )
    return [
        Rule(
            rule_id="rule-fire-001",
            doc_id="ad-b-2026",
            version="2026-ed1",
            section_path="AD-B/v2/2/2.3/b",
            category=Category.FIRE,
            title="Minimum fire door rating",
            text="Fire doors protecting a single stairway shall have a minimum "
            "fire resistance of 30 minutes (FD30).",
            normalized_form="fire door minimum resistance 30 minutes fd30 single stairway",
            parameters={"min_resistance_min": 30, "rating": "FD30"},
            references=["BS EN 1634-1", "BS 476-22"],
            valid_from=today,
            valid_to=None,
            recorded_at=now,
            status=RuleStatus.ACTIVE,
            citation=cite(
                42, "Fire doors ... shall have a minimum fire resistance of 30 minutes (FD30)."
            ),
            confidence=0.96,
            extraction_method="hybrid",
        ),
        Rule(
            rule_id="rule-struct-001",
            doc_id="ad-a-2026",
            version="2026-ed1",
            section_path="AD-A/v2/3/3.2/a",
            category=Category.STRUCTURE,
            title="Minimum escape route width",
            text="The minimum clear width of an escape route shall be 1,200 mm "
            "for buildings with more than 60 occupants.",
            normalized_form="escape route clear width 1200 mm 60 occupants",
            parameters={"min_width_mm": 1200, "occupant_threshold": 60},
            references=["BS EN 1991-1-1"],
            valid_from=today,
            valid_to=None,
            recorded_at=now,
            status=RuleStatus.ACTIVE,
            citation=cite(18, "The minimum clear width of an escape route shall be 1,200 mm ..."),
            confidence=0.98,
            extraction_method="deterministic",
        ),
        Rule(
            rule_id="rule-vent-001",
            doc_id="ad-f-2026",
            version="2026-ed1",
            section_path="AD-F/v1/1/1.4",
            category=Category.VENTILATION,
            title="Background ventilator area",
            text="Each habitable room shall have background ventilators with a "
            "total equivalent area of not less than 5,000 mm².",
            normalized_form="background ventilator area 5000 mm2 habitable room",
            parameters={"min_area_mm2": 5000},
            references=["BS EN 13141-1"],
            valid_from=today,
            valid_to=None,
            recorded_at=now,
            status=RuleStatus.ACTIVE,
            citation=cite(
                27, "Each habitable room shall have background ventilators ... 5,000 mm²."
            ),
            confidence=0.94,
            extraction_method="hybrid",
        ),
    ]


def _mock_change_sets() -> list[ChangeSet]:
    """Seed the mock with a realistic review queue: one applied, one pending."""
    now = datetime.now(UTC)
    yesterday = now.replace(hour=9, minute=15, second=0, microsecond=0)
    return [
        ChangeSet(
            change_set_id="cs-demo-001",
            doc_id="ad-b-2026",
            version="2026-ed1",
            created_at=yesterday,
            status="applied",
            reviewed_by="alice@practice.com",
            reviewed_at=now,
            review_notes="Verified against the published 2026 edition.",
            deltas=[
                RuleDelta(
                    new_rule_id="rule-fire-001",
                    matched_rule_id="rule-fire-000",
                    decision=Decision.PARTIAL,
                    confidence=0.91,
                    rationale="Fire resistance updated from 20 min to 30 min.",
                    diff_summary="min_resistance_min: 20 -> 30",
                ),
            ],
        ),
        ChangeSet(
            change_set_id="cs-demo-002",
            doc_id="ad-a-2026",
            version="2026-ed1",
            created_at=now,
            status="proposed",
            deltas=[
                RuleDelta(
                    new_rule_id="rule-struct-001",
                    matched_rule_id="rule-struct-000",
                    decision=Decision.PARTIAL,
                    confidence=0.88,
                    rationale="Escape route width increased from 1,000 mm to 1,200 mm.",
                    diff_summary="min_width_mm: 1000 -> 1200",
                ),
                RuleDelta(
                    new_rule_id="rule-vent-001",
                    matched_rule_id=None,
                    decision=Decision.NOVEL,
                    confidence=0.95,
                    rationale="New requirement for background ventilator area.",
                    diff_summary="Added: min_area_mm2 = 5000",
                ),
            ],
        ),
    ]


class MockBackend:
    def __init__(self) -> None:
        self._rules = _mock_rules()
        self._changes = _mock_change_sets()

    def _score(self, query: str, rule: Rule) -> float:
        q = query.lower()
        refs = " ".join(rule.references or [])
        haystack = f"{rule.title or ''} {rule.text} {rule.normalized_form} {refs}".lower()
        if not q:
            return 0.0
        terms = q.split()
        hits = sum(1 for t in terms if t in haystack)
        return hits / max(len(terms), 1)

    def search(self, query: str, top_k: int = 10, mode: SearchMode = "hybrid") -> list[RuleHit]:
        # Mock has no vector index; keyword scoring is the only leg available.
        # We accept `mode` for Protocol conformance but always rank by keywords.
        scored = [(r, self._score(query, r)) for r in self._rules]
        scored = [(r, s) for r, s in scored if s > 0] or [(r, 0.0) for r in self._rules[:top_k]]
        scored.sort(key=lambda rs: rs[1], reverse=True)
        return [RuleHit(r, s) for r, s in scored[:top_k]]

    def get_rule(self, rule_id: str) -> Rule | None:
        return next((r for r in self._rules if r.rule_id == rule_id), None)

    def get_current(self, topic: str | None = None, category: str | None = None) -> list[Rule]:
        rules = self._rules
        if category:
            rules = [r for r in rules if r.category.value == category]
        if topic:
            tl = topic.lower()
            rules = [
                r for r in rules if tl in (r.text or "").lower() or tl in (r.title or "").lower()
            ]
        return rules

    def get_figure(self, rule_id: str) -> Figure | None:
        return None

    def list_changes(self, status: str | None = None, limit: int = 50) -> list[ChangeSet]:
        out = self._changes
        if status:
            out = [c for c in out if c.status == status]
        return list(out[:limit])

    def get_change_set(self, change_set_id: str) -> ChangeSet | None:
        return next((c for c in self._changes if c.change_set_id == change_set_id), None)

    def review_change_set(
        self, change_set_id: str, decision: str, reviewer: str, notes: str = ""
    ) -> ChangeSet | None:
        valid = {"applied", "rejected", "rolled_back"}
        if decision not in valid:
            raise ValueError(f"decision must be one of {valid}, got '{decision}'")
        for i, c in enumerate(self._changes):
            if c.change_set_id == change_set_id:
                self._changes[i] = c.model_copy(
                    update={
                        "status": decision,
                        "reviewed_by": reviewer,
                        "reviewed_at": datetime.now(UTC),
                        "review_notes": notes,
                    }
                )
                return self._changes[i]
        return None

    def get_ops_stats(self) -> PipelineStats:
        """Mock stats derived from the in-memory seed data."""
        active = [r for r in self._rules if r.status == RuleStatus.ACTIVE]
        rules_by_status: dict[str, int] = {}
        for r in self._rules:
            rules_by_status[r.status.value] = rules_by_status.get(r.status.value, 0) + 1
        rules_by_category: dict[str, int] = {}
        for r in active:
            rules_by_category[r.category.value] = rules_by_category.get(r.category.value, 0) + 1
        cs_by_status: dict[str, int] = {}
        for c in self._changes:
            cs_by_status[c.status] = cs_by_status.get(c.status, 0) + 1
        return PipelineStats(
            active_rules=len(active),
            total_rules=len(self._rules),
            rules_by_status=rules_by_status,
            rules_by_category=rules_by_category,
            documents=3,
            document_versions=3,
            change_sets_by_status=cs_by_status,
            pending_review=cs_by_status.get("proposed", 0),
        )

    def diff(self, rule_id: str) -> dict:
        rule = self.get_rule(rule_id)
        if not rule:
            return {"error": "not found", "rule_id": rule_id}
        return {
            "rule_id": rule_id,
            "current": _rule_to_dict(rule),
            "previous": {
                "min_resistance_min": 20,
                "rating": "FD20",
                "note": "Prior value superseded by 2026 edition.",
            },
            "change": "min_resistance_min: 20 -> 30",
        }

    def list_audit_events(
        self,
        *,
        entity: str | None = None,
        entity_id: str | None = None,
        actor: str | None = None,
        action: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """Mock audit trail — a static seed so the UI works without a DB."""
        now = datetime.now(UTC)
        seed = [
            {
                "id": 1,
                "actor": "pipeline@system",
                "action": "ingest",
                "entity": "document",
                "entity_id": "ad-b-2026",
                "payload": {"version": "2026-ed1", "rules_extracted": 1},
                "at": now.replace(hour=9, minute=15),
            },
            {
                "id": 2,
                "actor": "alice@practice.com",
                "action": "apply",
                "entity": "change_set",
                "entity_id": "cs-demo-001",
                "payload": {"notes": "Verified against the published 2026 edition."},
                "at": now,
            },
        ]
        out = seed
        if entity:
            out = [e for e in out if e["entity"] == entity]
        if entity_id:
            out = [e for e in out if e["entity_id"] == entity_id]
        if actor:
            out = [e for e in out if e["actor"] == actor]
        if action:
            out = [e for e in out if e["action"] == action]
        for e in out:
            e["at"] = e["at"].isoformat()
        return out[offset : offset + limit]


# --------------------------------------------------------------------------- #
#  Helpers — serialise domain models to plain dicts for JSON
# --------------------------------------------------------------------------- #
def _citation_to_dict(c: Citation) -> dict:
    return {
        "doc_id": c.doc_id,
        "version": c.version,
        "page": c.page,
        "raw_text": c.raw_text,
        "url": c.url,
        "short": c.short(),
    }


def _rule_to_dict(r: Rule) -> dict:
    return {
        "rule_id": r.rule_id,
        "doc_id": r.doc_id,
        "version": r.version,
        "section_path": r.section_path,
        "category": r.category.value,
        "title": r.title,
        "text": r.text,
        "parameters": r.parameters,
        "references": r.references,
        "valid_from": r.valid_from.isoformat(),
        "valid_to": r.valid_to.isoformat() if r.valid_to else None,
        "status": r.status.value,
        "confidence": r.confidence,
        "extraction_method": r.extraction_method,
        "citation": _citation_to_dict(r.citation),
        "is_current": r.is_current(),
    }


def rules_to_dicts(rules: Sequence[Rule]) -> list[dict]:
    return [_rule_to_dict(r) for r in rules]


def changeset_to_dict(cs: ChangeSet) -> dict:
    return {
        "change_set_id": cs.change_set_id,
        "doc_id": cs.doc_id,
        "version": cs.version,
        "created_at": cs.created_at.isoformat(),
        "reviewed_at": cs.reviewed_at.isoformat() if cs.reviewed_at else None,
        "reviewed_by": cs.reviewed_by,
        "review_notes": cs.review_notes,
        "status": cs.status,
        "delta_count": len(cs.deltas),
        "deltas": [
            {
                "new_rule_id": d.new_rule_id,
                "matched_rule_id": d.matched_rule_id,
                "decision": d.decision.value,
                "confidence": d.confidence,
                "rationale": d.rationale,
                "diff_summary": d.diff_summary,
            }
            for d in cs.deltas
        ],
    }


def changesets_to_dicts(css: Sequence[ChangeSet]) -> list[dict]:
    return [changeset_to_dict(c) for c in css]


# --------------------------------------------------------------------------- #
#  Factory
# --------------------------------------------------------------------------- #
def _select_backend() -> ContextBackend:
    """Pick a backend once: Mock unless Postgres is reachable, then Real.

    This is the underlying implementation; callers should use
    :func:`get_backend`, which memoises the result for the process lifetime
    via :func:`functools.lru_cache`.
    """
    import os

    if os.environ.get("UK_REGS_BACKEND", "").lower() == "mock":
        return MockBackend()
    try:
        from uk_regs_knowledge.relational.connection import get_pool

        get_pool()  # raises if DB unreachable
        return RealBackend()
    except Exception:
        # DB not available -> degrade gracefully to mock
        return MockBackend()


@lru_cache(maxsize=1)
def get_backend() -> ContextBackend:
    """
    Process-wide singleton :class:`ContextBackend`.

    Memoised with :func:`functools.lru_cache` so the (potentially expensive)
    DB-reachability probe in :func:`_select_backend` runs at most once per
    process. The returned instance is shared across all requests; both
    ``RealBackend`` and ``MockBackend`` are stateless w.r.t. search reads, and
    writes go through their own pooled connections, so sharing is safe.

    If the selected backend must be changed at runtime (e.g. for tests),
    call ``get_backend.cache_clear()``.
    """
    return _select_backend()
