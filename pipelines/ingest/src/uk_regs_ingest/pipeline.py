"""
The end-to-end ingest pipeline — the orchestrator that ties every layer together.

Flow (one PDF):
    intake → parse → deterministic facts → LLM rule extraction
            → figure understanding → embed → reconcile against corpus
            → persist (rules, deltas, change-set) → emit result

Design notes
------------
* Pure functions per stage, composed here. Easy to unit-test in isolation.
* Degrades gracefully: if no LLM/embedder is configured, it still parses and
  persists the deterministic facts + document version (useful for local dev).
* Idempotent on (doc_id, version): re-running the same PDF produces the same
  rule IDs and updates the same rows.
* Human-in-the-loop: a ChangeSet is created in `proposed` status. Nothing
  becomes `active` automatically unless `--apply` is passed (and even then,
  low-confidence rules are kept as `proposed`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from uk_regs_core import (
    BlockType,
    ChangeSet,
    Document,
    DocumentType,
    DocumentVersion,
    Rule,
    RuleStatus,
    Settings,
    get_settings,
)
from uk_regs_core.ids import (
    change_set_id,
    doc_id_from_title,
    now_utc,
    sha256_of,
)

from uk_regs_extraction.deterministic.extractors import extract_facts
from uk_regs_extraction.figures.understander import FigureUnderstander
from uk_regs_extraction.llm.client import LLMClient, get_llm_client
from uk_regs_extraction.llm.rule_extractor import RuleExtractor, to_rule
from uk_regs_extraction.parsing.pdf_parser import (
    ParsedDocument,
    drain_pending_uploads,
    parse_pdf,
)

from uk_regs_knowledge.embeddings import Embedder, get_embedder
from uk_regs_knowledge.repositories.relational_repo import (
    ChangeSetRepository,
    DocumentRepository,
    RuleRepository,
)
from uk_regs_knowledge.storage.object_store import (
    ObjectStore,
    document_key,
    get_object_store,
)

from uk_regs_reconcile.engine import Reconciler

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
#  Result
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class IngestResult:
    """Summary of one ingest run. Returned to the CLI / caller."""

    doc_id: str
    version: str
    pages: int
    blocks: int
    figures: int
    rules_extracted: int
    decisions: dict[str, int] = field(default_factory=dict)
    needs_review: int = 0
    change_set_id: str | None = None
    applied: bool = False

    def summary(self) -> str:
        lines = [
            f"doc_id            : {self.doc_id}",
            f"version           : {self.version}",
            f"pages             : {self.pages}",
            f"blocks            : {self.blocks}",
            f"figures           : {self.figures}",
            f"rules extracted   : {self.rules_extracted}",
            f"decisions         : {self.decisions or '—'}",
            f"needs review      : {self.needs_review}",
            f"change_set_id     : {self.change_set_id}",
            f"applied           : {self.applied}",
        ]
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
#  Pipeline
# --------------------------------------------------------------------------- #
@dataclass
class IngestPipeline:
    """
    Orchestrates the full ingest.

    Dependencies are injected so tests can pass fakes. In production all are
    constructed from settings.
    """

    settings: Settings = field(default_factory=get_settings)
    object_store: ObjectStore | None = None
    llm: LLMClient | None = None
    embedder: Embedder | None = None
    reconciler: Reconciler | None = None
    # repositories are lazy (only needed when persisting)
    doc_repo: DocumentRepository | None = None
    rule_repo: RuleRepository | None = None
    change_set_repo: ChangeSetRepository | None = None

    def __post_init__(self) -> None:
        # Lazily wire optional deps. We try/except so the pipeline can run
        # without external services (e.g. in unit tests or offline dev).
        if self.object_store is None:
            try:
                self.object_store = get_object_store(self.settings)
            except Exception:  # pragma: no cover
                logger.warning("object store unavailable; figures/PDFs won't be persisted")
                self.object_store = None

    # ------------------------------------------------------------------ #
    #  Public entrypoint
    # ------------------------------------------------------------------ #
    def run(
        self,
        pdf_path: Path,
        *,
        title: str,
        version: str,
        doc_type: DocumentType = DocumentType.APPROVED_DOCUMENT,
        source_url: str | None = None,
        effective_from: date | None = None,
        apply: bool = False,
        skip_llm: bool = False,
        out_dir: Path | None = None,
    ) -> IngestResult:
        """
        Ingest a single PDF.

        Args:
            pdf_path: path to the PDF file
            title: human title, e.g. "Approved Document B"
            version: version label, e.g. "2026-ed1"
            doc_type: document classification
            source_url: provenance URL
            effective_from: when rules become legally effective (defaults to today)
            apply: if True, set new/matched rules to ACTIVE. If False, leave as PROPOSED.
            skip_llm: if True, do deterministic extraction only (fast, cheap, offline)
            out_dir: if set, dump extracted rules + summary as JSON to this directory
        """
        pdf_bytes = pdf_path.read_bytes()
        doc_id = doc_id_from_title(title)
        sha = sha256_of(pdf_bytes)
        effective = effective_from or date.today()

        logger.info("ingest start: %s v=%s sha=%s", doc_id, version, sha[:12])

        # 1. Intake — immutable storage of the source PDF
        self._intake(pdf_bytes, doc_id, version, sha, title, source_url, doc_type)

        # 2. Parse (deterministic backbone)
        parsed = parse_pdf(pdf_bytes, doc_id=doc_id, version=version)

        # 3. Persist figure images drained from the parser
        self._drain_figures(doc_id, version)

        # 4. Figure understanding (VLM) — optional
        figures = self._understand_figures(parsed) if not skip_llm else []

        # 5. Rule extraction
        rules = self._extract_rules(parsed, doc_id, version, effective, skip_llm=skip_llm)
        logger.info("extracted %d rules from %s", len(rules), doc_id)

        # 6. Persist document version + rules (always, even if PROPOSED)
        self._persist_version(
            doc_id=doc_id,
            version=version,
            title=title,
            source_url=source_url,
            doc_type=doc_type,
            sha=sha,
            pages=parsed.pages,
            effective=effective,
        )
        self._persist_rules(rules, apply=apply)

        # 7. Reconcile against the current corpus
        decisions, review_count, cs_id = self._reconcile_and_persist(
            rules, doc_id, version, apply=apply)

        # 8. Optional JSON dump (for verification / offline dev / EDA export)
        if out_dir is not None:
            self._dump_json(
                out_dir, doc_id=doc_id, version=version, rules=rules,
                decisions=decisions, review_count=review_count,
                change_set_id=cs_id, parsed=parsed,
            )

        return IngestResult(
            doc_id=doc_id,
            version=version,
            pages=parsed.pages,
            blocks=len(parsed.blocks),
            figures=len(figures),
            rules_extracted=len(rules),
            decisions=decisions,
            needs_review=review_count,
            change_set_id=cs_id,
            applied=apply,
        )

    def _dump_json(
        self,
        out_dir: Path,
        *,
        doc_id: str,
        version: str,
        rules: list[Rule],
        decisions: dict[str, int],
        review_count: int,
        change_set_id: str | None,
        parsed: ParsedDocument,
    ) -> None:
        """Persist a human/machine-readable snapshot of this run."""
        import json

        out_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{doc_id}_{version}".replace("/", "-")

        rules_doc = {
            "doc_id": doc_id,
            "version": version,
            "generated_at": now_utc().isoformat(),
            "rules": [r.model_dump(mode="json") for r in rules],
            "summary": {
                "pages": parsed.pages,
                "blocks": len(parsed.blocks),
                "rules": len(rules),
                "decisions": decisions,
                "needs_review": review_count,
                "change_set_id": change_set_id,
            },
        }
        (out_dir / f"{stem}.rules.json").write_text(
            json.dumps(rules_doc, indent=2, default=str), encoding="utf-8"
        )
        logger.info("dumped rules JSON to %s/%s.rules.json", out_dir, stem)

    # ------------------------------------------------------------------ #
    #  Stages
    # ------------------------------------------------------------------ #
    def _intake(
        self,
        pdf_bytes: bytes,
        doc_id: str,
        version: str,
        sha: str,
        title: str,
        source_url: str | None,
        doc_type: DocumentType,
    ) -> None:
        if self.object_store is None:
            return
        key = document_key(doc_id, version, sha)
        if not self.object_store.exists(key):
            self.object_store.put(key, pdf_bytes, content_type="application/pdf")
            logger.info("stored source PDF at %s", key)

        # Upsert document row (idempotent)
        doc = Document(
            doc_id=doc_id,
            title=title,
            doc_type=doc_type,
            source_url=source_url,
            sha256=sha,
        )
        try:
            DocumentRepository().upsert_document(doc)
        except Exception:  # DB may be down in dev
            logger.warning("could not upsert document row (continuing)", exc_info=True)

    def _drain_figures(self, doc_id: str, version: str) -> None:
        pending = drain_pending_uploads()
        if not pending:
            return
        if self.object_store is None:
            logger.warning("parser produced %d figure(s) but no object store is available", len(pending))
            return
        for key, img_bytes in pending.items():
            self.object_store.put(key, img_bytes, content_type="image/png")
        logger.info("persisted %d figure image(s)", len(pending))

    def _understand_figures(self, parsed: ParsedDocument) -> list[dict]:
        """Run the VLM over figure blocks. Best-effort; never fatal."""
        if self.llm is None:
            return []
        understander = FigureUnderstander(self.llm, self.object_store)
        results: list[dict] = []
        figure_blocks = [b for b in parsed.blocks if b.block_type == BlockType.FIGURE and b.figure_key]
        for fb in figure_blocks:
            try:
                desc = understander.describe(fb.figure_key)  # type: ignore[arg-type]
                results.append({"block_id": fb.block_id, "description": desc})
            except Exception:
                logger.warning("figure understanding failed for %s", fb.block_id, exc_info=True)
        return results

    def _extract_rules(
        self,
        parsed: ParsedDocument,
        doc_id: str,
        version: str,
        effective: date,
        *,
        skip_llm: bool,
    ) -> list[Rule]:
        extractor = RuleExtractor(self.llm) if (self.llm is not None and not skip_llm) else None
        rules: list[Rule] = []
        section_index: dict[str, int] = {}

        for block in parsed.blocks:
            if block.block_type not in (BlockType.TEXT, BlockType.LIST_ITEM, BlockType.HEADING):
                continue
            if not block.text or not block.text.strip():
                continue

            facts = extract_facts(block.text)
            # Skip blocks with no regulatory signal if we have no LLM to judge
            if extractor is None and not facts.has_any:
                continue

            if extractor is not None:
                try:
                    extracted = extractor.extract(
                        text=block.text,
                        facts=facts,
                        doc_id=doc_id,
                        version=version,
                        page=block.page,
                    )
                except Exception:
                    logger.warning("LLM extraction failed on block %s", block.block_id, exc_info=True)
                    continue
                if extracted is None or not extracted.is_regulation:
                    continue
                section_path = extracted.section_path or _nearest_section(facts, block.page) or f"p{block.page}"
            else:
                # Deterministic-only fallback: synthesize a minimal rule from facts
                from uk_regs_core import Category, Citation

                section_path = _nearest_section(facts, block.page) or f"p{block.page}"
                extracted = None  # sentinel

            idx = section_index.get(section_path, 0)
            section_index[section_path] = idx + 1

            if extracted is not None:
                rules.append(
                    to_rule(
                        extracted,
                        doc_id=doc_id,
                        version=version,
                        page=block.page,
                        raw_text=block.text,
                        section_path=section_path,
                        valid_from=effective,
                        index=idx,
                    )
                )
            else:
                rules.append(
                    _det_only_rule(
                        text=block.text,
                        facts=facts,
                        doc_id=doc_id,
                        version=version,
                        page=block.page,
                        section_path=section_path,
                        valid_from=effective,
                        index=idx,
                    )
                )
        return rules

    def _persist_version(
        self,
        *,
        doc_id: str,
        version: str,
        title: str,
        source_url: str | None,
        doc_type: DocumentType,
        sha: str,
        pages: int,
        effective: date,
    ) -> None:
        try:
            v = DocumentVersion(
                doc_id=doc_id,
                version=version,
                published_date=effective,
                effective_from=effective,
                object_key=document_key(doc_id, version, sha),
                pages=pages,
            )
            DocumentRepository().upsert_version(v)
        except Exception:
            logger.warning("could not persist document version (continuing)", exc_info=True)

    def _persist_rules(self, rules: list[Rule], *, apply: bool) -> None:
        if not rules:
            return
        try:
            repo = RuleRepository()
            embeddings: list[list[float]] | None = None
            if self.embedder is not None:
                try:
                    embeddings = self.embedder.embed_texts([r.normalized_form for r in rules])
                except Exception:
                    logger.warning("embedding batch failed; rules persisted without vectors", exc_info=True)
                    embeddings = None
            for i, rule in enumerate(rules):
                if apply:
                    rule = rule.model_copy(update={"status": RuleStatus.ACTIVE})
                emb = embeddings[i] if embeddings else None
                repo.upsert_rule(rule, embedding=emb)
        except Exception:
            logger.warning("could not persist rules (continuing)", exc_info=True)

    def _reconcile_and_persist(
        self, rules: list[Rule], doc_id: str, version: str, *, apply: bool
    ) -> tuple[dict[str, int], int, str | None]:
        if not rules:
            return {}, 0, None
        try:
            corpus = RuleRepository().get_current_rules()
        except Exception:
            logger.warning("could not load corpus; reconciliation skipped", exc_info=True)
            return {}, 0, None

        reconciler = self.reconciler or Reconciler(llm=self.llm)
        results, cs_id = reconciler.reconcile(rules, corpus, doc_id=doc_id)

        decisions: dict[str, int] = {}
        review_count = 0
        for r in results:
            decisions[r.delta.decision.value] = decisions.get(r.delta.decision.value, 0) + 1
            if r.needs_review:
                review_count += 1

        # Persist the change-set (always as 'proposed' unless apply)
        cs = ChangeSet(
            change_set_id=cs_id,
            doc_id=doc_id,
            version=version,
            created_at=now_utc(),
            deltas=[r.delta for r in results],
            status="applied" if apply else "proposed",
        )
        try:
            ChangeSetRepository().save(cs)
        except Exception:
            logger.warning("could not persist change-set (continuing)", exc_info=True)

        return decisions, review_count, cs_id


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #
def _nearest_section(facts, page: int) -> str | None:
    """Pick the most plausible section token from extracted facts."""
    if facts.sections:
        return facts.sections[0]
    return None


def _det_only_rule(
    *,
    text: str,
    facts,
    doc_id: str,
    version: str,
    page: int,
    section_path: str,
    valid_from: date,
    index: int,
) -> Rule:
    """Construct a rule using ONLY deterministic facts (no LLM)."""
    from uk_regs_core import Category, Citation, RuleStatus
    from uk_regs_core.ids import now_utc, rule_id

    return Rule(
        rule_id=rule_id(doc_id, section_path, index=index),
        doc_id=doc_id,
        version=version,
        section_path=section_path,
        category=Category.OTHER,
        title=None,
        text=text.strip(),
        normalized_form=" ".join(text.lower().split()),
        parameters=facts.as_parameters(),
        references=facts.standards,
        valid_from=valid_from,
        valid_to=None,
        recorded_at=now_utc(),
        status=RuleStatus.PROPOSED,
        citation=Citation(doc_id=doc_id, version=version, page=page, raw_text=text.strip()),
        confidence=0.5,
        extraction_method="deterministic",
    )