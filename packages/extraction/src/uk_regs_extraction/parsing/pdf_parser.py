"""
Layout-aware PDF parsing.

Strategy: PyMuPDF (fitz) for speed + reliable bbox/page recovery.
This module is the *deterministic backbone* — it extracts raw blocks and
figures. Semantic interpretation happens downstream (deterministic + LLM).

For scanned/legacy docs, an OCR pass (see ocr.py) augments text blocks.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image

import fitz  # PyMuPDF

from uk_regs_core import BlockType, BoundingBox, ContentBlock
from uk_regs_core.ids import ulid_like


# PyMuPDF block flags
_LIST_ITEM_FLAGS = 1  # not authoritative; we re-detect list items via regex
_IMAGE_BLOCK = 1


@dataclass(slots=True)
class ParsedDocument:
    """Result of parsing one PDF version."""

    doc_id: str
    version: str
    pages: int
    blocks: list[ContentBlock]
    figure_block_ids: list[str]


def parse_pdf(
    pdf_bytes: bytes,
    *,
    doc_id: str,
    version: str,
    page_dpi: int = 150,
) -> ParsedDocument:
    """
    Parse a PDF into structured ContentBlocks.

    Args:
        pdf_bytes: raw PDF bytes
        doc_id: stable document id
        version: version label
        page_dpi: DPI for rasterising figure regions (saved as images)

    Returns:
        ParsedDocument with text + figure blocks, in reading order.
    """
    blocks: list[ContentBlock] = []
    figure_ids: list[str] = []

    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        for page_no, page in enumerate(doc, start=1):
            page_dict = page.get_text("dict")

            # ---- Text spans -> aggregate into lines/blocks ----
            raw_blocks = page_dict.get("blocks", [])
            reading_order = 0
            for rb in raw_blocks:
                bx0, by0, bx1, by1 = rb["bbox"]
                bbox = BoundingBox(x0=bx0, y0=by0, x1=bx1, y1=by1)

                if rb.get("type", 0) == _IMAGE_BLOCK:
                    # rasterised figure: rasterise the region for the VLM
                    img_key = _render_block_image(page, bbox, page_dpi, doc_id, version)
                    bid = ulid_like()
                    blocks.append(
                        ContentBlock(
                            block_id=bid,
                            doc_id=doc_id,
                            version=version,
                            page=page_no,
                            bbox=bbox,
                            block_type=BlockType.FIGURE,
                            text=None,
                            figure_key=img_key,
                            reading_order=reading_order,
                        )
                    )
                    figure_ids.append(bid)
                    reading_order += 1
                    continue

                # text block: concatenate line text
                text = _extract_block_text(rb)
                if not text.strip():
                    continue

                btype = _classify_text_block(text, rb)
                bid = ulid_like()
                blocks.append(
                    ContentBlock(
                        block_id=bid,
                        doc_id=doc_id,
                        version=version,
                        page=page_no,
                        bbox=bbox,
                        block_type=btype,
                        text=text,
                        reading_order=reading_order,
                    )
                )
                reading_order += 1

        pages = doc.page_count

    # sort by (page, top, left) to approximate reading order within a page
    blocks.sort(key=lambda b: (b.page, b.bbox.y0, b.bbox.x0))
    for i, b in enumerate(blocks):
        b = b.model_copy(update={"reading_order": i})
        blocks[i] = b

    return ParsedDocument(doc_id=doc_id, version=version, pages=pages, blocks=blocks, figure_block_ids=figure_ids)


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #
def _extract_block_text(rb: dict) -> str:
    parts: list[str] = []
    for line in rb.get("lines", []):
        spans = [span.get("text", "") for span in line.get("spans", [])]
        parts.append("".join(spans).strip())
    return "\n".join(p for p in parts if p)


def _render_block_image(
    page: fitz.Page, bbox: BoundingBox, dpi: int, doc_id: str, version: str
) -> str:
    """
    Rasterise a region of the page to PNG.

    NOTE: in production this is uploaded to object storage; the returned key
    is a placeholder path. See knowledge.storage for the real upload.
    """
    clip = fitz.Rect(bbox.as_tuple())
    pix = page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72), clip=clip)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    key = f"figures/{doc_id}/{version}/{ulid_like()}.png"
    # caller (pipeline) uploads buf.getvalue() to storage under `key`
    _PENDING_UPLOADS[key] = buf.getvalue()
    return key


# module-level staging area for rendered images, drained by the pipeline
_PENDING_UPLOADS: dict[str, bytes] = {}


def drain_pending_uploads() -> dict[str, bytes]:
    """Return and clear staged figure images."""
    global _PENDING_UPLOADS
    pending = _PENDING_UPLOADS
    _PENDING_UPLOADS = {}
    return pending


def _classify_text_block(text: str, rb: dict) -> BlockType:
    t = text.strip()
    # Heading: short line, large font
    if len(t) < 120 and _is_heading(rb):
        return BlockType.HEADING
    # Footnote: starts with a superscript-style marker
    if t[:2] in {"fn", "F."} or t.startswith("†"):
        return BlockType.FOOTNOTE
    # List item heuristic
    if t[:2] in {"• ", "- ", "* "} or (len(t) > 2 and t[0].isdigit() and t[1] in ")."):
        return BlockType.LIST_ITEM
    return BlockType.TEXT


def _is_heading(rb: dict) -> bool:
    try:
        line = rb["lines"][0]
        span = line["spans"][0]
        size = span.get("size", 0)
        flags = span.get("flags", 0)
        return size >= 13.0 or bool(flags & 2 ** 4)  # bold bit
    except (KeyError, IndexError):
        return False