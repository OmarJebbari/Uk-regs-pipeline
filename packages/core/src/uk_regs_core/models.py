"""
Core domain models for the UK Building Regulations pipeline.

These are THE single source of truth. Every service (API, MCP, pipeline, UI)
imports from here — no duplication, no drift.

All rules are citation-bearing and bitemporal (valid_time + transaction_time).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from uk_regs_core.enums import (
    BlockType,
    Category,
    Decision,
    DocumentType,
    RuleStatus,
)


# --------------------------------------------------------------------------- #
#  Citation — provenance for every fact
# --------------------------------------------------------------------------- #
class BoundingBox(BaseModel):
    """Axis-aligned bounding box on a PDF page, in PDF units (points)."""

    model_config = ConfigDict(frozen=True)

    x0: float = Field(ge=0)
    y0: float = Field(ge=0)
    x1: float = Field(ge=0)
    y1: float = Field(ge=0)

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.x0, self.y0, self.x1, self.y1)


class Citation(BaseModel):
    """Provenance: where a fact came from in a source document."""

    model_config = ConfigDict(frozen=True)

    doc_id: str
    version: str
    page: int = Field(ge=1)
    bbox: BoundingBox | None = None
    raw_text: str = Field(description="Verbatim source snippet backing the claim")
    url: str | None = None

    def short(self) -> str:
        loc = f"p.{self.page}"
        return f"{self.doc_id} {self.version} {loc}"


# --------------------------------------------------------------------------- #
#  Documents & versions
# --------------------------------------------------------------------------- #
class Document(BaseModel):
    """A source PDF (e.g. Approved Document B, 2026 edition)."""

    model_config = ConfigDict(frozen=True)

    doc_id: str
    title: str
    publisher: str = "gov.uk"
    doc_type: DocumentType = DocumentType.APPROVED_DOCUMENT
    source_url: str | None = None
    sha256: str
    license: str | None = None


class DocumentVersion(BaseModel):
    """A specific version/edition of a document."""

    model_config = ConfigDict(frozen=True)

    doc_id: str
    version: str = Field(description="e.g. '2026-ed1'")
    published_date: date
    effective_from: date | None = None
    object_key: str = Field(description="Object-store key for the immutable PDF")
    pages: int | None = None


# --------------------------------------------------------------------------- #
#  Parsed content blocks
# --------------------------------------------------------------------------- #
class ContentBlock(BaseModel):
    """A layout block recovered from a PDF (text, table cell, figure)."""

    model_config = ConfigDict(frozen=True)

    block_id: str
    doc_id: str
    version: str
    page: int
    bbox: BoundingBox
    block_type: BlockType
    text: str | None = None
    # For figures: stored image reference (object key) + VLM description
    figure_key: str | None = None
    figure_description: str | None = None
    reading_order: int = 0


# --------------------------------------------------------------------------- #
#  Rule — the central entity
# --------------------------------------------------------------------------- #
class Rule(BaseModel):
    """
    A structured, citation-bearing regulatory rule.

    Bitemporal:
      - valid_from/valid_to  : when the rule is legally in force
      - recorded_at          : when the system learned this rule

    Old rules are NEVER deleted — they are superseded.
    """

    rule_id: str
    doc_id: str
    version: str
    section_path: str = Field(description="e.g. 'AD-B/v2/2/2.3/b'")
    category: Category
    title: str | None = None
    text: str
    normalized_form: str = Field(description="Canonicalised text for matching")
    parameters: dict[str, Any] = Field(
        default_factory=dict,
        description="Typed numeric constraints, e.g. {'min_width_mm': 1200}",
    )
    references: list[str] = Field(
        default_factory=list,
        description="Standards referenced, e.g. ['BS EN 1991-1-1']",
    )
    # Bitemporal
    valid_from: date
    valid_to: date | None = None
    recorded_at: datetime
    status: RuleStatus = RuleStatus.ACTIVE
    # Provenance
    citation: Citation
    # QA
    confidence: float = Field(ge=0.0, le=1.0, default=1.0)
    extraction_method: str = Field(
        default="llm",
        description="'deterministic' | 'llm' | 'hybrid'",
    )

    @property
    def is_current(self, now: date | None = None) -> bool:
        today = now or date.today()
        if self.status != RuleStatus.ACTIVE:
            return False
        if self.valid_from > today:
            return False
        return self.valid_to is None or today < self.valid_to


# --------------------------------------------------------------------------- #
#  Reconciliation
# --------------------------------------------------------------------------- #
class RuleDelta(BaseModel):
    """One new rule compared against the existing corpus."""

    model_config = ConfigDict(frozen=True)

    new_rule_id: str
    matched_rule_id: str | None = None
    decision: Decision
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str
    diff_summary: str = Field(default="", description="Human-readable delta")


class ChangeSet(BaseModel):
    """
    Atomic unit of corpus update. One PDF -> one ChangeSet.
    Applied wholesale or rolled back wholesale — never half-applied.
    """

    change_set_id: str
    doc_id: str
    version: str
    created_at: datetime
    deltas: list[RuleDelta] = Field(default_factory=list)
    status: str = Field(
        default="proposed",
        description="proposed | validated | applied | rejected | rolled_back",
    )
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    review_notes: str | None = None


# --------------------------------------------------------------------------- #
#  Figures
# --------------------------------------------------------------------------- #
class FigureElement(BaseModel):
    """A discrete element detected inside a diagram (label, dimension, arrow)."""

    model_config = ConfigDict(frozen=True)

    element_type: str = Field(description="'label' | 'dimension' | 'arrow' | 'zone'")
    text: str | None = None
    value: str | None = None
    bbox: BoundingBox | None = None


class Figure(BaseModel):
    """A diagram that illustrates one or more rules."""

    figure_id: str
    doc_id: str
    version: str
    page: int
    caption: str | None = None
    image_key: str = Field(description="Object-store key for the raw image")
    description: str = Field(description="VLM-generated natural-language description")
    elements: list[FigureElement] = Field(default_factory=list)
    illustrates_rule_ids: list[str] = Field(default_factory=list)
    citation: Citation
