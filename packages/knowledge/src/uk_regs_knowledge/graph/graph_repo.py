"""
Neo4j graph repository for rules, references, dependencies, supersessions.

The graph answers questions like:
  - "What rules depend on BS EN 1991-1-1?"
  - "Show me the full version history of rule X"
  - "What did clause 2.3 look like across all editions?"

Postgres holds the truth for current-state queries; Neo4j shines at
multi-hop provenance/impact analysis.
"""

from __future__ import annotations

from uk_regs_core import Rule
from uk_regs_core.ids import now_utc

from uk_regs_knowledge.graph.connection import get_driver


class GraphRepository:
    def __init__(self, driver=None) -> None:
        self._driver = driver or get_driver()

    def close(self) -> None:
        self._driver.close()

    # -- write -------------------------------------------------------------- #
    def upsert_rule_node(self, rule: Rule) -> None:
        with self._driver.session() as s:
            s.run(
                """
                MERGE (r:Rule {rule_id: $rule_id})
                SET r.doc_id        = $doc_id,
                    r.version       = $version,
                    r.section_path  = $section_path,
                    r.category      = $category,
                    r.text          = $text,
                    r.norm          = $normalized,
                    r.status        = $status,
                    r.valid_from    = $valid_from,
                    r.valid_to      = $valid_to,
                    r.confidence    = $confidence,
                    r.page          = $page
                """,
                rule_id=rule.rule_id,
                doc_id=rule.doc_id,
                version=rule.version,
                section_path=rule.section_path,
                category=rule.category.value,
                text=rule.text,
                normalized=rule.normalized_form,
                status=rule.status.value,
                valid_from=str(rule.valid_from),
                valid_to=str(rule.valid_to) if rule.valid_to else None,
                confidence=rule.confidence,
                page=rule.citation.page,
            )

    def add_standard_nodes(self, standards: list[str]) -> None:
        with self._driver.session() as s:
            for std in standards:
                s.run(
                    "MERGE (:Standard {ref: $ref})",
                    ref=std,
                )

    def link_rule_to_standards(self, rule_id: str, standards: list[str]) -> None:
        with self._driver.session() as s:
            for std in standards:
                s.run(
                    """
                    MATCH (r:Rule {rule_id: $rid}), (s:Standard {ref: $ref})
                    MERGE (r)-[:REFERENCES]->(s)
                    """,
                    rid=rule_id,
                    ref=std,
                )

    def add_supersession(
        self,
        *,
        old_rule_id: str,
        new_rule_id: str,
        change_set_id: str,
        reason: str,
    ) -> None:
        with self._driver.session() as s:
            s.run(
                """
                MATCH (old:Rule {rule_id: $old}), (new:Rule {rule_id: $new})
                MERGE (old)-[e:SUPERSEDED_BY]->(new)
                SET e.change_set_id = $cs, e.reason = $reason, e.at = $at
                """,
                old=old_rule_id,
                new=new_rule_id,
                cs=change_set_id,
                reason=reason,
                at=now_utc().isoformat(),
            )

    def add_dependency(self, depending_rule_id: str, depended_rule_id: str) -> None:
        with self._driver.session() as s:
            s.run(
                """
                MATCH (a:Rule {rule_id: $a}), (b:Rule {rule_id: $b})
                MERGE (a)-[:DEPENDS_ON]->(b)
                """,
                a=depending_rule_id,
                b=depended_rule_id,
            )

    # -- read ---------------------------------------------------------------- #
    def rules_referencing_standard(self, ref: str) -> list[dict]:
        with self._driver.session() as s:
            result = s.run(
                """
                MATCH (r:Rule)-[:REFERENCES]->(:Standard {ref: $ref})
                RETURN r.rule_id AS rule_id, r.section_path AS section,
                       r.text AS text, r.status AS status
                """,
                ref=ref,
            )
            return [dict(rec) for rec in result]

    def version_history(self, rule_id: str) -> list[dict]:
        """Walk the supersession chain backwards from a rule."""
        with self._driver.session() as s:
            result = s.run(
                """
                MATCH path = (older)-[:SUPERSEDED_BY*]-> (current:Rule {rule_id: $rid})
                UNWIND [n IN nodes(path)] AS n
                RETURN DISTINCT n.rule_id AS rule_id, n.version AS version,
                       n.section_path AS section, n.text AS text,
                       n.status AS status, n.valid_from AS valid_from,
                       n.valid_to AS valid_to
                ORDER BY n.valid_from
                """,
                rid=rule_id,
            )
            return [dict(rec) for rec in result]

    def downstream_impact(self, rule_id: str) -> list[dict]:
        """Rules that DEPEND_ON the given rule — useful before superseding."""
        with self._driver.session() as s:
            result = s.run(
                """
                MATCH (dep:Rule)-[:DEPENDS_ON]->(:Rule {rule_id: $rid})
                RETURN dep.rule_id AS rule_id, dep.section_path AS section
                """,
                rid=rule_id,
            )
            return [dict(rec) for rec in result]
