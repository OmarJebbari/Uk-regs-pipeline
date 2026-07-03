"""
Figure understanding via Vision LLM.

Takes a rendered figure image and produces:
  - a natural-language description (for RAG + IDE context)
  - a structured list of elements (labels, dimensions, zones, arrows)

The description + raw image are both stored, so the IDE can retrieve either.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

from pydantic import BaseModel, Field

from uk_regs_core import BoundingBox, Citation, Figure, FigureElement
from uk_regs_core.ids import ulid_like

from uk_regs_extraction.llm.client import LLMClient


VLM_SYSTEM_PROMPT = """You are analysing a figure from a UK Building Regulations document.
Describe what the diagram shows and extract any explicit constraints shown.

Produce:
1. `description`: a faithful, detailed natural-language description.
2. `elements`: discrete labelled items you can see, each with:
   - element_type: one of 'label' | 'dimension' | 'arrow' | 'zone'
   - text: the visible label/text (verbatim)
   - value: the numeric value if it's a dimension (as a string)

Do NOT infer values that are not visibly written. If unsure, omit the element.
"""


class VLMMatch(BaseModel):
    """Structured VLM output for a figure."""

    description: str
    elements: list[FigureElement] = Field(default_factory=list)


class FigureUnderstander:
    """Wraps the VLM to interpret diagrams."""

    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm

    def understand(
        self,
        *,
        image_bytes: bytes,
        doc_id: str,
        version: str,
        page: int,
        caption: str | None = None,
    ) -> Figure:
        img_b64 = base64.b64encode(image_bytes).decode("ascii")
        user = f"Caption (if any): {caption or '(none)'}"
        result = self._llm.complete(
            system=VLM_SYSTEM_PROMPT,
            user=user,
            response_model=VLMMatch,
            image_b64=img_b64,
        )
        # In a full pipeline we would also describe_image for richer detail;
        # structured extraction above is the primary signal.
        return Figure(
            figure_id=ulid_like(),
            doc_id=doc_id,
            version=version,
            page=page,
            caption=caption,
            image_key="",  # set by storage layer on upload
            description=result.description,
            elements=result.elements,
            illustrates_rule_ids=[],
            citation=Citation(
                doc_id=doc_id,
                version=version,
                page=page,
                raw_text=caption or "",
            ),
        )