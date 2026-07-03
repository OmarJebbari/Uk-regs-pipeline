"""
Postgres repository for documents, rules, figures, change-sets.

Uses psycopg 3 with a connection pool. All writes are transactional.
Vector operations use pgvector's `<=>` cosine distance operator.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import date
from typing import Iterator

from uk_regs_core import (
    BoundingBox,
    Category,
    ChangeSet,
    Citation,
    Document,
    DocumentVersion,
    Figure,
    Rule,
    RuleDelta,
    RuleStatus,
    Settings,
    get_settings,
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
                    {', embedding = EXCLUDED.embedding' if embedding else ''}
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

    def _row_to_rule(self, row) -> Rule:
        cols = [d[0] for d in row.cursor.description]
        r = dict(zip(cols, row))
        return Rule(
            rule_id=r["rule_id"],
            doc_id=r["doc_id"],
            version=r["version"],
            section_path=r["section_path"],
            category=Category(r["category"]),
            title=r["title"],
            text=r["text"],
            normalized_form=r["normalized_form"],
            parameters=r["parameters"] if isinstance(r["parameters"], dict) else json.loads(r["parameters"]),
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