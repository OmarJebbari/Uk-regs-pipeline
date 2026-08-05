"""
SourceMonitor — the orchestrator that turns "new PDFs available" into an
ingest run.

Flow (per source):
    1. list_documents()   via the fetcher
    2. diff against seen-state
    3. for each new/changed document:
         a. fetch() bytes
         b. write to a temp file
         c. call the existing IngestPipeline.run()
         d. mark_seen() on success
    4. set_last_poll()

The monitor is intentionally pure about *what* it runs — it delegates the
actual extraction/reconciliation to IngestPipeline. This keeps the two concerns
decoupled: sourcing knows nothing about rule extraction, only PDFs + state.
"""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from uk_regs_sourcing.fetchers import make_fetcher
from uk_regs_sourcing.models import (
    DocumentDiff,
    FetchResult,
    SourceEntry,
    SourceState,
)
from uk_regs_sourcing.state import InMemoryStateStore, StateStore, get_state_store

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
#  Result
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class SourceRunResult:
    """Outcome of polling one source."""

    source_id: str
    advertised: int = 0
    new: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    ingested_doc_ids: list[str] = field(default_factory=list)
    change_set_ids: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"[{self.source_id}] advertised={self.advertised} "
            f"new={self.new} updated={self.updated} skipped={self.skipped} "
            f"failed={self.failed}"
        )


@dataclass(slots=True)
class MonitorResult:
    """Outcome of one monitor cycle across all sources."""

    started_at: datetime
    finished_at: datetime | None = None
    sources: list[SourceRunResult] = field(default_factory=list)

    @property
    def total_ingested(self) -> int:
        return sum(s.new + s.updated for s in self.sources)

    def summary(self) -> str:
        lines = [f"monitor cycle {self.started_at.isoformat()}"]
        for s in self.sources:
            lines.append("  " + s.summary())
        lines.append(f"total ingested: {self.total_ingested}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
#  Monitor
# --------------------------------------------------------------------------- #
@dataclass
class SourceMonitor:
    """
    Coordinates fetcher → diff → ingest for all enabled sources.

    Dependencies are injectable for testing; in prod they default to the
    global state store + the real IngestPipeline.
    """

    state_store: StateStore = field(default_factory=get_state_store)
    # Ingest pipeline factory: callable that returns a fresh IngestPipeline.
    # Injected so tests can pass a fake. Defaults to constructing the real one.
    pipeline_factory: object = None

    def __post_init__(self) -> None:
        if self.state_store is None:
            self.state_store = InMemoryStateStore()
        if self.pipeline_factory is None:
            # Lazy import to avoid a hard dependency at module import time.
            from uk_regs_ingest.pipeline import IngestPipeline

            self.pipeline_factory = IngestPipeline

    # ------------------------------------------------------------------ #
    #  Public API
    # ------------------------------------------------------------------ #
    def poll_source(
        self,
        entry: SourceEntry,
        *,
        force: bool = False,
        apply: bool = False,
        skip_llm: bool = False,
    ) -> SourceRunResult:
        """
        Poll a single source and ingest any new/changed documents.

        Args:
            entry: the source to poll
            force: if True, ignore poll_interval_minutes and re-ingest even unchanged
            apply: passed through to IngestPipeline (auto-apply change-sets)
            skip_llm: passed through to IngestPipeline (deterministic-only)
        """
        result = SourceRunResult(source_id=entry.source_id)
        fetcher = make_fetcher(entry)

        # Throttle: respect poll_interval unless forced
        if not force:
            last = self.state_store.last_poll(entry.source_id)
            if last is not None:
                elapsed_min = (datetime.now(UTC) - last).total_seconds() / 60.0
                if elapsed_min < entry.poll_interval_minutes:
                    result.skipped = -1  # sentinel: throttled
                    logger.info(
                        "source %s throttled (%.0f < %d min since last poll)",
                        entry.source_id,
                        elapsed_min,
                        entry.poll_interval_minutes,
                    )
                    return result

        # 1. List advertised documents
        try:
            advertised = fetcher.list_documents()
        except Exception as exc:  # noqa: BLE001
            result.failed = 1
            result.errors.append(f"list_documents failed: {exc}")
            logger.warning("source %s list_documents failed", entry.source_id, exc_info=True)
            return result
        result.advertised = len(advertised)

        # 2. Diff
        seen = self.state_store.get_seen(entry.source_id)
        diffs = [self._diff(state, seen, force=force) for state in advertised]

        # 3. Ingest each new/changed doc
        for d in diffs:
            if d.action == "skip":
                result.skipped += 1
                continue
            try:
                fetched = fetcher.fetch(d.state)
                cs_id = self._ingest(entry, fetched, apply=apply, skip_llm=skip_llm)
                self.state_store.mark_seen(entry.source_id, d.state)
                result.ingested_doc_ids.append(d.state.document_id)
                if cs_id:
                    result.change_set_ids.append(cs_id)
                if d.action == "update":
                    result.updated += 1
                else:
                    result.new += 1
            except Exception as exc:  # noqa: BLE001
                result.failed += 1
                result.errors.append(f"{d.state.document_id}: {exc}")
                logger.warning(
                    "ingest failed for %s/%s", entry.source_id, d.state.document_id, exc_info=True
                )

        # 4. Record the poll
        self.state_store.set_last_poll(entry.source_id, datetime.now(UTC))
        logger.info(result.summary())
        return result

    def poll_all(
        self,
        entries: list[SourceEntry],
        *,
        force: bool = False,
        apply: bool = False,
        skip_llm: bool = False,
    ) -> MonitorResult:
        """Poll every entry in the registry."""
        cycle = MonitorResult(started_at=datetime.now(UTC))
        for entry in entries:
            if not entry.enabled:
                continue
            cycle.sources.append(
                self.poll_source(entry, force=force, apply=apply, skip_llm=skip_llm)
            )
        cycle.finished_at = datetime.now(UTC)
        return cycle

    # ------------------------------------------------------------------ #
    #  Internals
    # ------------------------------------------------------------------ #
    @staticmethod
    def _diff(
        state: SourceState,
        seen: dict[str, SourceState],
        *,
        force: bool,
    ) -> DocumentDiff:
        """Decide whether a document should be ingested."""
        if force:
            return DocumentDiff(
                document_id=state.document_id,
                action="update" if state.document_id in seen else "ingest",
                reason="forced re-ingest",
                state=state,
            )
        prev = seen.get(state.document_id)
        if prev is None:
            return DocumentDiff(
                document_id=state.document_id,
                action="ingest",
                reason="new document",
                state=state,
            )
        # Changed? compare sha256 if known, else version + size.
        changed = (
            (state.sha256 is not None and prev.sha256 is not None and state.sha256 != prev.sha256)
            or (state.version != prev.version)
            or (state.size_bytes is not None and state.size_bytes != prev.size_bytes)
        )
        if changed:
            return DocumentDiff(
                document_id=state.document_id,
                action="update",
                reason="content/version changed",
                state=state,
            )
        return DocumentDiff(
            document_id=state.document_id,
            action="skip",
            reason="unchanged",
            state=state,
        )

    def _ingest(
        self,
        entry: SourceEntry,
        fetched: FetchResult,
        *,
        apply: bool,
        skip_llm: bool,
    ) -> str | None:
        """Materialise the PDF to a temp file and run the ingest pipeline."""
        from uk_regs_core.enums import DocumentType

        # Map the string doc_type to the enum; default to APPROVED_DOCUMENT.
        try:
            doc_type = DocumentType(entry.doc_type)
        except ValueError:
            doc_type = DocumentType.APPROVED_DOCUMENT

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:  # noqa: SIM115
            tmp.write(fetched.content)
            tmp_path = Path(tmp.name)
        try:
            pipeline = self._build_pipeline()
            res = pipeline.run(
                tmp_path,
                title=fetched.title,
                version=fetched.version,
                doc_type=doc_type,
                source_url=fetched.url,
                apply=apply,
                skip_llm=skip_llm,
            )
            return res.change_set_id
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                logger.debug("could not delete temp file %s", tmp_path)

    def _build_pipeline(self):  # type: ignore[no-untyped-def]
        factory = self.pipeline_factory
        # If it's a class, instantiate; if it's a callable factory, call it.
        if isinstance(factory, type):
            return factory()
        return factory()
