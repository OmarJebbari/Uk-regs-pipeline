"""
Postgres repository for documents, rules, figures, change-sets.

Uses psycopg 3 with a connection pool. All writes are transactional.
Vector operations use pgvector's `<=>` cosine distance operator.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date

from uk_regs_core import (
    Category,
    ChangeSet,
    Citation,
    Document,
    DocumentVersion,
    Figure,
    FigureElement,
    Rule,
    RuleDelta,
    RuleStatus,
)

from uk_regs_knowledge.relational.connection import get_pool


def _escape_like(query: str) -> str:
    """Escape LIKE/ILIKE wildcard characters in a user-supplied query.

    Escapes ``%``, ``_``, and the default escape char ``\\`` so that a query
    containing these characters is matched literally rather than interpreted
    as wildcards. The result is intended to be wrapped with surrounding
    ``%`` characters and used with an explicit ``ESCAPE '\\'`` clause.
    """
    return query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# --------------------------------------------------------------------------- #
#  Base
# --------------------------------------------------------------------------- #
class BaseRepository:
    """Shared connection-pool plumbing for all Postgres repositories."""

    def __init__(self, pool=None) -> None:
        self._pool = pool or get_pool()

    @contextmanager
    def _conn(self) -> Iterator:
        """Borrow a pooled connection, commit on success, rollback on error.

        Uses psycopg 3's ``pool.connection()`` context manager, which checks
        out a connection, commits on a clean exit, rolls back on error, and
        returns the connection to the pool automatically.
        """
        with self._pool.connection() as conn:
            yield conn


# --------------------------------------------------------------------------- #
#  Documents
# --------------------------------------------------------------------------- #
class DocumentRepository(BaseRepository):
    def upsert_document(self, doc: Document) -> None:
        with self._conn() as c, c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO documents (doc_id, title, publisher, doc_type, source_url, sha256, license)
                VALUES (%(doc_id)s, %(title)s, %(publisher)s, %(doc_type)s, %(source_url)s,
                        %(sha256)s, %(license)s)
                ON CONFLICT (doc_id) DO UPDATE SET
                    title      = EXCLUDED.title,
                    publisher  = EXCLUDED.publisher,
                    doc_type   = EXCLUDED.doc_type,
                    source_url = EXCLUDED.source_url,
                    sha256     = EXCLUDED.sha256,
                    license    = EXCLUDED.license
                """,
                {
                    "doc_id": doc.doc_id,
                    "title": doc.title,
                    "publisher": doc.publisher,
                    "doc_type": doc.doc_type.value,
                    "source_url": doc.source_url,
                    "sha256": doc.sha256,
                    "license": doc.license,
                },
            )

    def upsert_version(self, v: DocumentVersion) -> None:
        with self._conn() as c, c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO document_versions
                    (doc_id, version, published_date, effective_from, object_key, pages)
                VALUES (%(doc_id)s, %(version)s, %(published_date)s, %(effective_from)s,
                        %(object_key)s, %(pages)s)
                ON CONFLICT (doc_id, version) DO UPDATE SET
                    published_date = EXCLUDED.published_date,
                    effective_from = EXCLUDED.effective_from,
                    object_key     = EXCLUDED.object_key,
                    pages          = EXCLUDED.pages
                """,
                {
                    "doc_id": v.doc_id,
                    "version": v.version,
                    "published_date": v.published_date,
                    "effective_from": v.effective_from,
                    "object_key": v.object_key,
                    "pages": v.pages,
                },
            )

    def get_version_object_key(self, doc_id: str, version: str) -> str | None:
        """Object key for a stored (doc_id, version), or None if not seen before.

        Used by the ingest pipeline to skip re-processing byte-identical PDFs.
        """
        with self._conn() as c, c.cursor() as cur:
            cur.execute(
                "SELECT object_key FROM document_versions"
                " WHERE doc_id = %(doc_id)s AND version = %(version)s",
                {"doc_id": doc_id, "version": version},
            )
            row = cur.fetchone()
            return row[0] if row else None

    def count_documents(self) -> int:
        """Total number of distinct documents."""
        with self._conn() as c, c.cursor() as cur:
            cur.execute("SELECT count(*) FROM documents")
            return cur.fetchone()[0]

    def count_versions(self) -> int:
        """Total number of document versions (documents × versions)."""
        with self._conn() as c, c.cursor() as cur:
            cur.execute("SELECT count(*) FROM document_versions")
            return cur.fetchone()[0]


# --------------------------------------------------------------------------- #
#  Rules
# --------------------------------------------------------------------------- #
class RuleRepository(BaseRepository):
    def upsert_rule(self, rule: Rule, embedding: list[float] | None = None) -> None:
        emb_sql = ", embedding"
        emb_val = ", %s"
        params: list = [
            rule.rule_id,
            rule.doc_id,
            rule.version,
            rule.section_path,
            rule.category.value,
            rule.title,
            rule.text,
            rule.normalized_form,
            json.dumps(rule.parameters),
            rule.references,
            rule.valid_from,
            rule.valid_to,
            rule.recorded_at,
            rule.status.value,
            rule.citation.doc_id,
            rule.citation.version,
            rule.citation.page,
            rule.citation.raw_text,
            rule.citation.url,
            rule.confidence,
            rule.extraction_method,
        ]
        if embedding is None:
            emb_sql = ""
            emb_val = ""
        else:
            params.append(embedding)

        with self._conn() as c, c.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO rules (
                    rule_id, doc_id, version, section_path, category, title, text,
                    normalized_form, parameters, references, valid_from, valid_to,
                    recorded_at, status,
                    citation_doc_id, citation_version, citation_page,
                    citation_raw_text, citation_url,
                    confidence, extraction_method{emb_sql}
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s{emb_val}
                )
                ON CONFLICT (rule_id) DO UPDATE SET
                    text            = EXCLUDED.text,
                    normalized_form = EXCLUDED.normalized_form,
                    parameters      = EXCLUDED.parameters,
                    references      = EXCLUDED.references,
                    valid_from      = EXCLUDED.valid_from,
                    valid_to        = EXCLUDED.valid_to,
                    status          = EXCLUDED.status,
                    confidence      = EXCLUDED.confidence
                    {", embedding = EXCLUDED.embedding" if embedding else ""}
                """,
                params,
            )

    def total_count(self) -> int:
        """Total number of rules in the store (any status/version)."""
        with self._conn() as c, c.cursor() as cur:
            cur.execute("SELECT count(*) FROM rules")
            return cur.fetchone()[0]

    def count_by_status(self) -> dict[str, int]:
        """Rule counts grouped by status (active, superseded, withdrawn, …)."""
        with self._conn() as c, c.cursor() as cur:
            cur.execute("SELECT status, count(*) FROM rules GROUP BY status ORDER BY status")
            return dict(cur.fetchall())

    def count_by_category(self) -> dict[str, int]:
        """Active rule counts grouped by category."""
        with self._conn() as c, c.cursor() as cur:
            cur.execute(
                """
                SELECT category, count(*) FROM rules
                WHERE status = 'active'
                GROUP BY category ORDER BY category
                """
            )
            return dict(cur.fetchall())

    def get_current_rules(self, *, now: date | None = None) -> list[Rule]:
        """All rules currently in force (the 'current truth')."""
        today = now or date.today()
        with self._conn() as c, c.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM rules
                WHERE status = 'active' AND valid_from <= %s
                  AND (valid_to IS NULL OR valid_to > %s)
                ORDER BY section_path
                """,
                (today, today),
            )
            return [self._row_to_rule(r) for r in cur.fetchall()]

    def search_semantic(self, query_vec: list[float], top_k: int = 10) -> list[tuple[Rule, float]]:
        """
        Pure semantic search via pgvector cosine distance.

        Returns ``(Rule, score)`` where *score* is in ``[0, 1]``:
        ``1 - cosine_distance`` (so 1.0 = identical, 0.0 = orthogonal).
        Normalised here so both legs of hybrid search are on the same scale.
        """
        with self._conn() as c, c.cursor() as cur:
            cur.execute(
                """
                SELECT *, 1 - (embedding <=> %s::vector) AS score
                FROM rules
                WHERE status = 'active' AND embedding IS NOT NULL
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                (query_vec, query_vec, top_k),
            )
            rows = cur.fetchall()
        return [(self._row_to_rule(r), float(r[-1])) for r in rows]

    def search_bm25(self, query: str, top_k: int = 10) -> list[tuple[Rule, float]]:
        """
        Keyword leg of hybrid search — PostgreSQL native full-text search.

        Uses the generated ``tsv`` column + ``ts_rank_cd`` (coverage-density
        ranking, the closest Postgres-native approximation of BM25) over a
        weighted tsvector (title A > text B > normalized_form C). The raw
        ``ts_rank_cd`` is unbounded, so scores are min-max normalised to
        ``[0, 1]`` within the result set before return.

        Falls back to ``search_fulltext`` (ILIKE) if the ``tsv`` column is not
        present (older deployments) so search never hard-fails mid-migration.
        """
        like = f"%{_escape_like(query.lower())}%"
        with self._conn() as c, c.cursor() as cur:
            try:
                cur.execute(
                    """
                    SELECT *, ts_rank_cd(tsv, plainto_tsquery('english', %s)) AS rank
                    FROM rules
                    WHERE status = 'active'
                      AND tsv @@ plainto_tsquery('english', %s)
                    ORDER BY rank DESC
                    LIMIT %s
                    """,
                    (query, query, top_k),
                )
                rows = cur.fetchall()
            except Exception:
                # tsv column missing (pre-migration DB) -> ILIKE fallback.
                cur.execute(
                    """
                    SELECT *,
                           (text ILIKE %s ESCAPE '\\')::int
                             + (title ILIKE %s ESCAPE '\\')::int * 2
                             + (normalized_form ILIKE %s ESCAPE '\\')::int AS rank
                    FROM rules
                    WHERE status = 'active'
                      AND (text ILIKE %s ESCAPE '\\'
                           OR title ILIKE %s ESCAPE '\\'
                           OR normalized_form ILIKE %s ESCAPE '\\')
                    ORDER BY rank DESC
                    LIMIT %s
                    """,
                    (like, like, like, like, like, like, top_k),
                )
                rows = cur.fetchall()
        if not rows:
            return []
        max_rank = max(float(r[-1]) for r in rows) or 1.0
        return [(self._row_to_rule(r), float(r[-1]) / max_rank) for r in rows]

    def get_rule_by_id(self, rule_id: str) -> Rule | None:
        """Fetch a single rule by its stable id (any status/version)."""
        with self._conn() as c, c.cursor() as cur:
            cur.execute("SELECT * FROM rules WHERE rule_id = %s", (rule_id,))
            row = cur.fetchone()
        return self._row_to_rule(row) if row else None

    def get_by_category(self, category: str, *, now: date | None = None) -> list[Rule]:
        """All current rules in a given Category (e.g. 'fire')."""
        today = now or date.today()
        with self._conn() as c, c.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM rules
                WHERE category = %s
                  AND status = 'active'
                  AND valid_from <= %s
                  AND (valid_to IS NULL OR valid_to > %s)
                ORDER BY section_path
                """,
                (category, today, today),
            )
            return [self._row_to_rule(r) for r in cur.fetchall()]

    def search_fulltext(self, query: str, top_k: int = 10) -> list[tuple[Rule, float]]:
        """
        Keyword fallback when no embeddings are available.
        Uses ILIKE over text + normalized_form + title; scores by crude relevance.
        Returns (rule, score) where score is in [0, 1].
        """
        like = f"%{_escape_like(query.lower())}%"
        with self._conn() as c, c.cursor() as cur:
            cur.execute(
                """
                SELECT *,
                       (text ILIKE %s ESCAPE '\\')::int
                         + (title ILIKE %s ESCAPE '\\')::int * 2
                         + (normalized_form ILIKE %s ESCAPE '\\')::int AS score
                FROM rules
                WHERE status = 'active'
                  AND (text ILIKE %s ESCAPE '\\'
                       OR title ILIKE %s ESCAPE '\\'
                       OR normalized_form ILIKE %s ESCAPE '\\')
                ORDER BY score DESC, section_path
                LIMIT %s
                """,
                (like, like, like, like, like, like, top_k),
            )
            rows = cur.fetchall()
        max_score = max((r[-1] for r in rows), default=1) or 1
        return [(self._row_to_rule(r), float(r[-1]) / max_score) for r in rows]

    def _row_to_rule(self, row) -> Rule:
        cols = [d[0] for d in row.cursor.description]
        r = dict(zip(cols, row, strict=False))
        return Rule(
            rule_id=r["rule_id"],
            doc_id=r["doc_id"],
            version=r["version"],
            section_path=r["section_path"],
            category=Category(r["category"]),
            title=r["title"],
            text=r["text"],
            normalized_form=r["normalized_form"],
            parameters=r["parameters"]
            if isinstance(r["parameters"], dict)
            else json.loads(r["parameters"]),
            references=list(r["references"]),
            valid_from=r["valid_from"],
            valid_to=r["valid_to"],
            recorded_at=r["recorded_at"],
            status=RuleStatus(r["status"]),
            citation=Citation(
                doc_id=r["citation_doc_id"],
                version=r["citation_version"],
                page=r["citation_page"],
                raw_text=r["citation_raw_text"],
                url=r["citation_url"],
            ),
            confidence=float(r["confidence"]),
            extraction_method=r["extraction_method"],
        )


# --------------------------------------------------------------------------- #
#  Change-sets
# --------------------------------------------------------------------------- #
class ChangeSetRepository(BaseRepository):
    def save(self, cs: ChangeSet) -> None:
        with self._conn() as c, c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO change_sets
                    (change_set_id, doc_id, version, created_at, status,
                     reviewed_by, reviewed_at, review_notes)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (change_set_id) DO UPDATE SET
                    status        = EXCLUDED.status,
                    reviewed_by   = EXCLUDED.reviewed_by,
                    reviewed_at   = EXCLUDED.reviewed_at,
                    review_notes  = EXCLUDED.review_notes
                """,
                (
                    cs.change_set_id,
                    cs.doc_id,
                    cs.version,
                    cs.created_at,
                    cs.status,
                    cs.reviewed_by,
                    cs.reviewed_at,
                    cs.review_notes,
                ),
            )
            for d in cs.deltas:
                cur.execute(
                    """
                    INSERT INTO rule_deltas
                        (change_set_id, new_rule_id, matched_rule_id, decision,
                         confidence, rationale, diff_summary)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (change_set_id, new_rule_id) DO UPDATE SET
                        decision     = EXCLUDED.decision,
                        confidence   = EXCLUDED.confidence,
                        rationale    = EXCLUDED.rationale,
                        diff_summary = EXCLUDED.diff_summary
                    """,
                    (
                        cs.change_set_id,
                        d.new_rule_id,
                        d.matched_rule_id,
                        d.decision.value,
                        d.confidence,
                        d.rationale,
                        d.diff_summary,
                    ),
                )

    def review(
        self,
        change_set_id: str,
        decision: str,
        reviewer: str,
        notes: str = "",
    ) -> ChangeSet | None:
        """
        Apply a human review decision to a change-set.

        ``decision`` is one of: 'applied' | 'rejected' | 'rolled_back'.
        Records the reviewer, timestamp, and notes, then returns the updated
        change-set. Returns ``None`` if the change-set does not exist.
        """
        from datetime import UTC, datetime

        valid = {"applied", "rejected", "rolled_back"}
        if decision not in valid:
            raise ValueError(f"decision must be one of {valid}, got '{decision}'")

        with self._conn() as c, c.cursor() as cur:
            cur.execute(
                """
                UPDATE change_sets
                SET status = %s, reviewed_by = %s, reviewed_at = %s, review_notes = %s
                WHERE change_set_id = %s
                """,
                (decision, reviewer, datetime.now(UTC), notes, change_set_id),
            )
            if cur.rowcount == 0:
                return None

            cur.execute(
                """
                SELECT * FROM change_sets WHERE change_set_id = %s
                """,
                (change_set_id,),
            )
            cs_row = cur.fetchone()
            if cs_row is None:
                return None
            cs_cols = [d[0] for d in cur.description]
            csd = dict(zip(cs_cols, cs_row, strict=False))

            cur.execute(
                "SELECT * FROM rule_deltas WHERE change_set_id = %s ORDER BY decision, new_rule_id",
                (csd["change_set_id"],),
            )
            d_rows = cur.fetchall()
            d_cols = [d[0] for d in cur.description]
            deltas: list[RuleDelta] = []
            for d_row in d_rows:
                dr = dict(zip(d_cols, d_row, strict=False))
                deltas.append(self._row_to_delta(dr))
        return self._row_to_changeset(csd, deltas)

    def count_by_status(self) -> dict[str, int]:
        """Change-set counts grouped by status (proposed, applied, …)."""
        with self._conn() as c, c.cursor() as cur:
            cur.execute("SELECT status, count(*) FROM change_sets GROUP BY status ORDER BY status")
            return dict(cur.fetchall())

    def get_by_id(self, change_set_id: str) -> ChangeSet | None:
        """
        Fetch a single change-set by id, with all its deltas.

        This is a direct primary-key lookup — far cheaper than the old
        ``list_changes(limit=1000)`` + linear scan that was used previously.
        Returns ``None`` if the change-set does not exist.
        """
        with self._conn() as c, c.cursor() as cur:
            cur.execute(
                "SELECT * FROM change_sets WHERE change_set_id = %s",
                (change_set_id,),
            )
            cs_row = cur.fetchone()
            if cs_row is None:
                return None
            cs_cols = [d[0] for d in cur.description]
            csd = dict(zip(cs_cols, cs_row, strict=False))

            cur.execute(
                "SELECT * FROM rule_deltas WHERE change_set_id = %s ORDER BY decision, new_rule_id",
                (csd["change_set_id"],),
            )
            d_rows = cur.fetchall()
            d_cols = [d[0] for d in cur.description]
            deltas: list[RuleDelta] = []
            for d_row in d_rows:
                dr = dict(zip(d_cols, d_row, strict=False))
                deltas.append(self._row_to_delta(dr))
        return self._row_to_changeset(csd, deltas)

    def list_changes(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> list[ChangeSet]:
        """List change-sets (optionally filtered by status), newest first."""
        if status:
            sql = "SELECT * FROM change_sets WHERE status = %s ORDER BY created_at DESC LIMIT %s"
            params: tuple = (status, limit)
        else:
            sql = "SELECT * FROM change_sets ORDER BY created_at DESC LIMIT %s"
            params = (limit,)

        with self._conn() as c, c.cursor() as cur:
            cur.execute(sql, params)
            cs_rows = cur.fetchall()
            cs_cols = [d[0] for d in cur.description]
            result: list[ChangeSet] = []
            for cs_row in cs_rows:
                csd = dict(zip(cs_cols, cs_row, strict=False))
                cur.execute(
                    "SELECT * FROM rule_deltas WHERE change_set_id = %s "
                    "ORDER BY decision, new_rule_id",
                    (csd["change_set_id"],),
                )
                d_rows = cur.fetchall()
                d_cols = [d[0] for d in cur.description]
                deltas: list[RuleDelta] = []
                for d_row in d_rows:
                    dr = dict(zip(d_cols, d_row, strict=False))
                    deltas.append(self._row_to_delta(dr))
                result.append(self._row_to_changeset(csd, deltas))
        return result

    @staticmethod
    def _row_to_delta(dr: dict) -> RuleDelta:
        """Hydrate a ``RuleDelta`` from a rule_deltas row dict."""
        from uk_regs_core.enums import Decision

        return RuleDelta(
            new_rule_id=dr["new_rule_id"],
            matched_rule_id=dr["matched_rule_id"],
            decision=Decision(dr["decision"]),
            confidence=float(dr["confidence"]),
            rationale=dr["rationale"],
            diff_summary=dr["diff_summary"],
        )

    @staticmethod
    def _row_to_changeset(csd: dict, deltas: list[RuleDelta]) -> ChangeSet:
        """Hydrate a ``ChangeSet`` from a change_sets row dict + deltas."""
        return ChangeSet(
            change_set_id=csd["change_set_id"],
            doc_id=csd["doc_id"],
            version=csd["version"],
            created_at=csd["created_at"],
            deltas=deltas,
            status=csd["status"],
            reviewed_by=csd["reviewed_by"],
            reviewed_at=csd["reviewed_at"],
            review_notes=csd["review_notes"],
        )


# --------------------------------------------------------------------------- #
#  Figures
# --------------------------------------------------------------------------- #
class FigureRepository(BaseRepository):
    """CRUD for diagrams linked to rules (stored in the ``figures`` table)."""

    def save(self, fig: Figure) -> None:
        with self._conn() as c, c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO figures (
                    figure_id, doc_id, version, page, caption, image_key,
                    description, elements, illustrates_rules,
                    citation_page, citation_raw_text
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
                )
                ON CONFLICT (figure_id) DO UPDATE SET
                    caption            = EXCLUDED.caption,
                    image_key          = EXCLUDED.image_key,
                    description        = EXCLUDED.description,
                    elements           = EXCLUDED.elements,
                    illustrates_rules  = EXCLUDED.illustrates_rules,
                    citation_page      = EXCLUDED.citation_page,
                    citation_raw_text  = EXCLUDED.citation_raw_text
                """,
                (
                    fig.figure_id,
                    fig.doc_id,
                    fig.version,
                    fig.page,
                    fig.caption,
                    fig.image_key,
                    fig.description,
                    json.dumps([e.model_dump() for e in fig.elements]),
                    fig.illustrates_rule_ids,
                    fig.citation.page,
                    fig.citation.raw_text,
                ),
            )

    def get_by_id(self, figure_id: str) -> Figure | None:
        with self._conn() as c, c.cursor() as cur:
            cur.execute("SELECT * FROM figures WHERE figure_id = %s", (figure_id,))
            row = cur.fetchone()
        return self._row_to_figure(row, cur) if row else None

    def list_for_rule(self, rule_id: str) -> list[Figure]:
        """All figures whose ``illustrates_rules`` array contains ``rule_id``."""
        with self._conn() as c, c.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM figures
                WHERE %s = ANY (illustrates_rules)
                ORDER BY page
                """,
                (rule_id,),
            )
            rows = cur.fetchall()
        return [self._row_to_figure(r, cur) for r in rows]

    def _row_to_figure(self, row, cur) -> Figure:
        cols = [d[0] for d in cur.description]
        r = dict(zip(cols, row, strict=False))
        raw_elements = r.get("elements") or []
        if isinstance(raw_elements, str):
            raw_elements = json.loads(raw_elements)
        elements = [FigureElement(**e) for e in raw_elements]
        return Figure(
            figure_id=r["figure_id"],
            doc_id=r["doc_id"],
            version=r["version"],
            page=r["page"],
            caption=r.get("caption"),
            image_key=r["image_key"],
            description=r["description"],
            elements=elements,
            illustrates_rule_ids=list(r.get("illustrates_rules") or []),
            citation=Citation(
                doc_id=r["doc_id"],
                version=r["version"],
                page=r["citation_page"],
                raw_text=r.get("citation_raw_text") or "",
                url=None,
            ),
        )


# --------------------------------------------------------------------------- #
#  Audit
# --------------------------------------------------------------------------- #
class AuditRepository(BaseRepository):
    """
    Append-only audit trail for all mutations (ingest, review, rollback).

    Every mutating API/pipeline action should call :meth:`log` so there is a
    complete, queryable history of who changed what, when, and why.
    """

    def log(
        self,
        *,
        actor: str,
        action: str,
        entity: str,
        entity_id: str,
        payload: dict | None = None,
    ) -> int:
        """
        Record an audit event.

        Args:
            actor: Who performed the action (user email, system, pipeline-run-id).
            action: What was done (``ingest``, ``apply``, ``reject``,
                ``rollback``, ``export``).
            entity: Table/entity affected (``change_set``, ``rule``,
                ``document``).
            entity_id: Primary key of the affected entity.
            payload: Arbitrary JSON metadata (before/after, rationale, …).

        Returns:
            The ``id`` of the newly inserted audit event.
        """
        with self._conn() as c, c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO audit_events (actor, action, entity, entity_id, payload)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    actor,
                    action,
                    entity,
                    entity_id,
                    json.dumps(payload or {}),
                ),
            )
            return cur.fetchone()[0]

    def list_events(
        self,
        *,
        entity: str | None = None,
        entity_id: str | None = None,
        actor: str | None = None,
        action: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """
        Query the audit trail with optional filters. Results are newest-first.

        Returns a list of dicts with keys: ``id``, ``actor``, ``action``,
        ``entity``, ``entity_id``, ``payload``, ``at``.
        """
        clauses: list[str] = []
        params: list = []
        if entity:
            clauses.append("entity = %s")
            params.append(entity)
        if entity_id:
            clauses.append("entity_id = %s")
            params.append(entity_id)
        if actor:
            clauses.append("actor = %s")
            params.append(actor)
        if action:
            clauses.append("action = %s")
            params.append(action)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([limit, offset])

        with self._conn() as c, c.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, actor, action, entity, entity_id, payload, at
                FROM audit_events
                {where}
                ORDER BY at DESC, id DESC
                LIMIT %s OFFSET %s
                """,
                params,
            )
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]

        results: list[dict] = []
        for row in rows:
            d = dict(zip(cols, row, strict=False))
            # payload comes back as a Python dict from psycopg's JSONB adapter
            if isinstance(d.get("payload"), str):
                d["payload"] = json.loads(d["payload"])
            results.append(d)
        return results

    def count_events(
        self,
        *,
        entity: str | None = None,
        action: str | None = None,
    ) -> int:
        """Total event count, optionally filtered (for dashboard stats)."""
        clauses: list[str] = []
        params: list = []
        if entity:
            clauses.append("entity = %s")
            params.append(entity)
        if action:
            clauses.append("action = %s")
            params.append(action)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._conn() as c, c.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM audit_events {where}", params)
            return cur.fetchone()[0]
