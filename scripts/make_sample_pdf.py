"""
Generate a small synthetic "Approved Document"-style PDF for end-to-end testing.

Produces data/sample.pdf with headings, regulatory text containing dimensions
and standards references, a list item, and an embedded figure.

Run:
    uv run python scripts/make_sample_pdf.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import fitz  # PyMuPDF

SAMPLE_RULES = [
    (
        "Section 2: Means of escape",
        "2.3 Every internal door on an escape route shall have a minimum clear\n"
        "opening width of not less than 1,200 mm in accordance with BS EN 1991-1-1.",
    ),
    (
        "Section 3: Fire resistance",
        "3.1 The minimum fire resistance period for loadbearing elements shall be\n"
        "60 minutes, complying with BS EN 13501-2.",
    ),
    (
        "Section 4: Headroom",
        "4.2 The minimum headroom on a stairway shall be not less than 2,000 mm.",
    ),
]


def build(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()

    # ---- Page 1 ----
    page = doc.new_page(width=595, height=842)  # A4

    y = 72
    page.insert_text(
        (72, y), "Approved Document B (Sample)", fontsize=20, fontname="helvetica-bold"
    )
    y += 40

    for heading, body in SAMPLE_RULES:
        page.insert_text((72, y), heading, fontsize=14, fontname="helvetica-bold")
        y += 24
        # body may have multiple lines
        for line in body.split("\n"):
            page.insert_text((80, y), line, fontsize=11, fontname="helvetica")
            y += 18
        y += 14

    # a list item (deterministic extractor should pick up the leading number)
    page.insert_text(
        (80, y),
        "5.1) Accessible entrances require a level threshold of 0 mm.",
        fontsize=11,
        fontname="helvetica",
    )

    # ---- Page 2: a simple vector "diagram" so the parser sees an image block ----
    page2 = doc.new_page(width=595, height=842)
    page2.insert_text(
        (72, 72), "Figure 2.1 — Escape route geometry", fontsize=14, fontname="helvetica-bold"
    )
    # Draw a rectangle representing a door opening (vector graphics -> embedded image)
    rect = fitz.Rect(150, 150, 450, 350)
    page2.draw_rect(rect, color=(0, 0, 0), width=2)
    page2.insert_text((160, 170), "min 1,200 mm clear width", fontsize=11, fontname="helvetica")

    doc.save(str(path))
    doc.close()
    print(f"wrote {path} ({path.stat().st_size} bytes)")


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/sample.pdf")
    build(out)
