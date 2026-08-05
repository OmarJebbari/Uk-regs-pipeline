"""
Pipeline statistics DTO for the ops dashboard (Phase 5-3).

A plain ``PipelineStats`` view-model aggregating counts from all subsystems
so the ops dashboard can render a single coherent picture without the route
handler needing to know about repository internals.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field


class PipelineStats(BaseModel):
    """Snapshot of pipeline state for the ops dashboard."""

    generated_at: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat(),
        description="ISO-8601 timestamp this snapshot was generated.",
    )

    # --- rules ---
    active_rules: int = 0
    total_rules: int = 0
    rules_by_status: dict[str, int] = Field(default_factory=dict)
    rules_by_category: dict[str, int] = Field(default_factory=dict)

    # --- documents ---
    documents: int = 0
    document_versions: int = 0

    # --- review queue ---
    change_sets_by_status: dict[str, int] = Field(default_factory=dict)
    pending_review: int = 0

    # --- sources ---
    sources_count: int = 0
    sources_enabled: int = 0

    def to_dict(self) -> dict:
        return self.model_dump()
