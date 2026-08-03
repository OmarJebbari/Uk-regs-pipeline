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


# --------------------------------------------------------------------------- #
#  Documents
# --------------------------------------------------------------------------- #
class DocumentRepository:
    def __init__(self, pool=None) -> None:
        self._pool = pool or get_pool()

    @contextmanager
    def _conn(self) -> Iterator:
        conn = self._pool.getconn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

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


# --------------------------------------------------------------------------- #
#  Rules
# --------------------------------------------------------------------------- #
class RuleRepository:
    def __init__(self, pool=None) -> None:
        self._pool = pool or get_pool()

    @contextmanager
    def _conn(self) -> Iterator:
        conn = self._pool.getconn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

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
        """Hybrid-ready: pure semantic via pgvector."""
        with self._conn() as c, c.cursor() as cur:
            cur.execute(
                """
                SELECT *, embedding <=> %s::vector AS distance
                FROM rules
                WHERE status = 'active'
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                (query_vec, query_vec, top_k),
            )
            rows = cur.fetchall()
        return [(self._row_to_rule(r), r[-1]) for r in rows]

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
        like = f"%{query.lower()}%"
        with self._conn() as c, c.cursor() as cur:
            cur.execute(
                """
                SELECT *,
                       (text ILIKE %s)::int + (title ILIKE %s)::int * 2
                         + (normalized_form ILIKE %s)::int AS score
                FROM rules
                WHERE status = 'active'
                  AND (text ILIKE %s OR title ILIKE %s OR normalized_form ILIKE %s)
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
class ChangeSetRepository:
    def __init__(self, pool=None) -> None:
        self._pool = pool or get_pool()

    @contextmanager
    def _conn(self) -> Iterator:
        conn = self._pool.getconn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

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

    def list_changes(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> list[ChangeSet]:
        """List change-sets (optionally filtered by status), newest first."""
        from uk_regs_core.enums import Decision

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
                    deltas.append(
                        RuleDelta(
                            new_rule_id=dr["new_rule_id"],
                            matched_rule_id=dr["matched_rule_id"],
                            decision=Decision(dr["decision"]),
                            confidence=float(dr["confidence"]),
                            rationale=dr["rationale"],
                            diff_summary=dr["diff_summary"],
                        )
                    )
                result.append(
                    ChangeSet(
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
                )
        return result


# --------------------------------------------------------------------------- #
#  Figures
# --------------------------------------------------------------------------- #
class FigureRepository:
    """CRUD for diagrams linked to rules (stored in the ``figures`` table)."""

    def __init__(self, pool=None) -> None:
        self._pool = pool or get_pool()

    @contextmanager
    def _conn(self) -> Iterator:
        conn = self._pool.getconn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

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
