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
from typing import Protocol

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
    def search(self, query: str, top_k: int = 10) -> list[RuleHit]: ...

    def get_rule(self, rule_id: str) -> Rule | None: ...

    def get_current(self, topic: str | None = None, category: str | None = None) -> list[Rule]: ...

    def get_figure(self, rule_id: str) -> Figure | None: ...

    def list_changes(self, status: str | None = None, limit: int = 50) -> list[ChangeSet]: ...

    def diff(self, rule_id: str) -> dict: ...


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

    def search(self, query: str, top_k: int = 10) -> list[RuleHit]:
        self._ensure()
        emb = self._get_embedder()
        if emb and emb != "unavailable":
            try:
                vec = emb.embed_text(query)
                rows = self._rules.search_semantic(vec, top_k=top_k)
                return [RuleHit(r, 1.0 - d) for r, d in rows]
            except Exception:
                pass  # fall through to fulltext
        # fulltext fallback
        rows = self._rules.search_fulltext(query, top_k=top_k)
        return [RuleHit(r, s) for r, s in rows]

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
        """Fallback: read the SQL `supersessions` mirror table."""
        from uk_regs_knowledge.relational.connection import get_pool

        pool = get_pool()
        conn = pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT old_rule_id, reason FROM supersessions WHERE new_rule_id = %s",
                    (rule_id,),
                )
                row = cur.fetchone()
            if not row:
                return None
            old_id, reason = row
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
        finally:
            pool.putconn(conn)


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


class MockBackend:
    def __init__(self) -> None:
        self._rules = _mock_rules()

    def _score(self, query: str, rule: Rule) -> float:
        q = query.lower()
        haystack = f"{rule.title or ''} {rule.text} {rule.normalized_form}".lower()
        if not q:
            return 0.0
        terms = q.split()
        hits = sum(1 for t in terms if t in haystack)
        return hits / max(len(terms), 1)

    def search(self, query: str, top_k: int = 10) -> list[RuleHit]:
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
        now = datetime.now(UTC)
        return [
            ChangeSet(
                change_set_id="cs-demo-001",
                doc_id="ad-b-2026",
                version="2026-ed1",
                created_at=now,
                status="applied",
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
            )
        ]

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
        "is_current": r.is_current,
    }


def rules_to_dicts(rules: Sequence[Rule]) -> list[dict]:
    return [_rule_to_dict(r) for r in rules]


def changeset_to_dict(cs: ChangeSet) -> dict:
    return {
        "change_set_id": cs.change_set_id,
        "doc_id": cs.doc_id,
        "version": cs.version,
        "created_at": cs.created_at.isoformat(),
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
def get_backend() -> ContextBackend:
    """Return a Mock backend unless Postgres is reachable, then Real."""
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
