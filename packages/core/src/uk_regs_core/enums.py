"""Enumerations shared across the whole system."""

from __future__ import annotations

from enum import Enum


class DocumentType(str, Enum):
    APPROVED_DOCUMENT = "approved_document"
    TECHNICAL_HANDBOOK = "technical_handbook"
    BRITISH_STANDARD = "british_standard"
    GUIDANCE = "guidance"
    OTHER = "other"


class Category(str, Enum):
    FIRE = "fire"
    STRUCTURE = "structure"
    VENTILATION = "ventilation"
    ACCESS = "access"
    DRAINAGE = "drainage"
    ENERGY = "energy"
    SOUND = "sound"
    SAFETY = "safety"
    OTHER = "other"


class RuleStatus(str, Enum):
    PROPOSED = "proposed"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    WITHDRAWN = "withdrawn"


class BlockType(str, Enum):
    TEXT = "text"
    HEADING = "heading"
    TABLE_CELL = "table_cell"
    FIGURE = "figure"
    FOOTNOTE = "footnote"
    LIST_ITEM = "list_item"
    PAGE_HEADER = "page_header"
    PAGE_FOOTER = "page_footer"


class Decision(str, Enum):
    """Reconciliation decisions."""

    MATCH = "match"        # unchanged
    PARTIAL = "partial"    # amended -> supersede old
    NOVEL = "novel"        # new rule
    CONFLICT = "conflict"  # contradictory -> human review


class ChangeSetStatus(str, Enum):
    PROPOSED = "proposed"
    VALIDATED = "validated"
    APPLIED = "applied"
    REJECTED = "rejected"
    ROLLED_BACK = "rolled_back"