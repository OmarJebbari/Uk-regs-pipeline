"""Enumerations shared across the whole system."""

from __future__ import annotations

from enum import StrEnum


class DocumentType(StrEnum):
    APPROVED_DOCUMENT = "approved_document"
    TECHNICAL_HANDBOOK = "technical_handbook"
    BRITISH_STANDARD = "british_standard"
    GUIDANCE = "guidance"
    OTHER = "other"


class Category(StrEnum):
    FIRE = "fire"
    STRUCTURE = "structure"
    VENTILATION = "ventilation"
    ACCESS = "access"
    DRAINAGE = "drainage"
    ENERGY = "energy"
    SOUND = "sound"
    SAFETY = "safety"
    OTHER = "other"


class RuleStatus(StrEnum):
    PROPOSED = "proposed"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    WITHDRAWN = "withdrawn"


class BlockType(StrEnum):
    TEXT = "text"
    HEADING = "heading"
    TABLE_CELL = "table_cell"
    FIGURE = "figure"
    FOOTNOTE = "footnote"
    LIST_ITEM = "list_item"
    PAGE_HEADER = "page_header"
    PAGE_FOOTER = "page_footer"


class Decision(StrEnum):
    """Reconciliation decisions."""

    MATCH = "match"  # unchanged
    PARTIAL = "partial"  # amended -> supersede old
    NOVEL = "novel"  # new rule
    CONFLICT = "conflict"  # contradictory -> human review


class ChangeSetStatus(StrEnum):
    PROPOSED = "proposed"
    VALIDATED = "validated"
    APPLIED = "applied"
    REJECTED = "rejected"
    ROLLED_BACK = "rolled_back"
