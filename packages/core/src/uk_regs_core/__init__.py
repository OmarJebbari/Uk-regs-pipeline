"""uk_regs_core — single source of truth for types, config, and errors."""

from uk_regs_core.config import Settings, get_settings
from uk_regs_core.enums import (
    BlockType,
    Category,
    ChangeSetStatus,
    Decision,
    DocumentType,
    RuleStatus,
)
from uk_regs_core.errors import (
    ChangeSetRejected,
    ConfigError,
    ExtractionError,
    ReconciliationError,
    StorageError,
    UkRegsError,
    UngroundedError,
)
from uk_regs_core.ids import (
    change_set_id,
    doc_id_from_title,
    now_utc,
    rule_id,
    sha256_of,
    slug,
    ulid_like,
)
from uk_regs_core.models import (
    BoundingBox,
)
from uk_regs_core.models import (
    ChangeSet,
    Citation,
    ContentBlock,
    Document,
    DocumentVersion,
    Figure,
    FigureElement,
    Rule,
    RuleDelta,
)

__all__ = [
    "Settings",
    "get_settings",
    # enums
    "BlockType",
    "Category",
    "ChangeSetStatus",
    "Decision",
    "DocumentType",
    "RuleStatus",
    # errors
    "ChangeSetRejected",
    "ConfigError",
    "ExtractionError",
    "ReconciliationError",
    "StorageError",
    "UkRegsError",
    "UngroundedError",
    # ids
    "change_set_id",
    "doc_id_from_title",
    "now_utc",
    "rule_id",
    "sha256_of",
    "slug",
    "ulid_like",
    # models
    "BoundingBox",
    "ChangeSet",
    "Citation",
    "ContentBlock",
    "Document",
    "DocumentVersion",
    "Figure",
    "FigureElement",
    "Rule",
    "RuleDelta",
]