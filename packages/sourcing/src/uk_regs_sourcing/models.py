"""
Declarative source configuration + runtime state models.

A *source* is an authoritative publisher of regulatory PDFs (e.g. the gov.uk
planning portal). Each source exposes a *fetcher* that returns the list of
documents it currently publishes; the monitor diffs that list against the
seen-state and ingests only the new/changed ones.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------- #
#  Registry — declarative configuration (sources.yaml)
# --------------------------------------------------------------------------- #
# Validated at runtime by make_fetcher(); kept as str so config loads even
# with an unknown kind (clear error comes from the factory, not Pydantic).
FetcherKind = str


class SourceEntry(BaseModel):
    """One watched source, e.g. gov.uk Approved Document B."""

    model_config = ConfigDict(frozen=True)

    source_id: str = Field(description="Stable id, e.g. 'gov-uk-adb'")
    title: str = Field(description="Human title, e.g. 'Approved Document B'")
    publisher: str = "gov.uk"
    fetcher: FetcherKind = "http"
    url: str | None = Field(
        default=None,
        description="Base URL for the http fetcher (manifest or directory page).",
    )
    path: str | None = Field(
        default=None,
        description="Local directory for the 'local' fetcher.",
    )
    doc_type: str = "approved_document"
    poll_interval_minutes: int = Field(
        default=1440,
        description="Minimum minutes between polls. 1440 = daily.",
    )
    enabled: bool = True
    # Metadata passed into the ingest pipeline when a doc is fetched
    default_version: str = Field(
        default="latest",
        description="Version label if the source does not expose one.",
    )


class SourceRegistry(BaseModel):
    """The full set of watched sources, loaded from sources.yaml."""

    sources: list[SourceEntry] = Field(default_factory=list)

    def enabled_sources(self) -> list[SourceEntry]:
        return [s for s in self.sources if s.enabled]

    def get(self, source_id: str) -> SourceEntry | None:
        for s in self.sources:
            if s.source_id == source_id:
                return s
        return None


# --------------------------------------------------------------------------- #
#  Runtime state
# --------------------------------------------------------------------------- #
class SourceState(BaseModel):
    """One document as advertised by a source at poll time."""

    model_config = ConfigDict(frozen=True)

    source_id: str
    document_id: str = Field(
        description=("Source-local document id (often the last URL segment or filename)."),
    )
    title: str
    version: str
    url: str
    sha256: str | None = Field(
        default=None,
        description=(
            "Content hash if known cheaply (e.g. from ETag/Content-Length). "
            "Used to short-circuit re-download."
        ),
    )
    size_bytes: int | None = None
    published_at: datetime | None = None


class FetchResult(BaseModel):
    """Result of fetching one document's bytes."""

    model_config = ConfigDict(frozen=True)

    document_id: str
    version: str
    title: str
    url: str
    content: bytes
    sha256: str
    source_id: str
    local_path: Path | None = Field(
        default=None,
        description="Path on disk if the fetcher materialised a temp file.",
    )


# --------------------------------------------------------------------------- #
#  Diff
# --------------------------------------------------------------------------- #
DiffAction = Literal["ingest", "skip", "update"]


class DocumentDiff(BaseModel):
    """The monitor's decision for one advertised document."""

    model_config = ConfigDict(frozen=True)

    document_id: str
    action: DiffAction
    reason: str
    state: SourceState
