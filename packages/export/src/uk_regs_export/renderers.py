"""
Static context renderers — the heart of Phase 6-1.

Each renderer is a **pure function**: ``Sequence[Rule] -> (filename, content)``.
No DB, no network, no side effects → trivially unit-testable and reusable from
the API, the CLI, or a CI job.

Output formats
--------------
* **Cursor ``.mdc``** — one file *per category*, with YAML frontmatter that
  scopes the rule set to relevant globs (so fire-safety rules only load when
  the engineer is editing fire-related code). This is Cursor's modern rules
  format; the older ``.cursorrules`` is a flat file we also emit.
* **``CLAUDE.md``** — a single repo-root brief for Claude Code / Claude
  Desktop. Dense, citation-bearing, ordered by category.
* **Consolidated Markdown** — a human-readable dump of every current rule.
* **JSON** — machine-readable, for programmatic consumers / diffing exports
  across versions.

Every emitted rule carries its citation (doc_id, version, page, raw snippet)
so an IDE assistant can ground every code change in a source page.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from uk_regs_core import Rule

# --------------------------------------------------------------------------- #
#  Cursor .mdc — per-category scoped rules
# --------------------------------------------------------------------------- #
# Map each category to the file globs that should trigger loading those rules.
# An engineer editing `src/fire/doors.ts` only pulls in fire-safety context —
# keeping the assistant's context window lean and the rules precise.
_CATEGORY_GLOBS: dict[str, list[str]] = {
    "fire": ["**/fire/**", "**/*fire*", "**/*escape*", "**/*door*"],
    "structure": ["**/struct*", "**/load*", "**/foundat*", "**/*beam*"],
    "ventilation": ["**/vent*", "**/air*", "**/*duct*"],
    "access": ["**/access*", "**/*ramp*", "**/*lift*", "**/*entrance*"],
    "drainage": ["**/drain*", "**/*waste*", "**/*sewer*"],
    "energy": ["**/energy*", "**/*uvalue*", "**/*insulat*", "**/*thermal*"],
    "sound": ["**/sound*", "**/*acoustic*", "**/*noise*"],
    "safety": ["**/safety*", "**/*guard*", "**/*barrier*", "**/*stair*"],
    "other": [],
}


def render_cursor_mdc(rules: list[Rule]) -> dict[str, str]:
    """
    Render one ``.mdc`` rule file per category that has rules.

    Returns a mapping of ``filename -> content``. Filenames follow Cursor's
    convention: ``.cursor/rules/<category>.mdc``.

    Each file begins with YAML frontmatter:

        ---
        description: UK Building Regulations — <Category> (current truth)
        globs: ["**/fire/**", ...]
        alwaysApply: false
        ---

    so Cursor only injects the relevant ruleset into the assistant's context
    when the engineer is editing matching files.
    """
    by_cat: dict[str, list[Rule]] = defaultdict(list)
    for r in rules:
        by_cat[r.category.value].append(r)

    out: dict[str, str] = {}
    for cat, cat_rules in sorted(by_cat.items()):
        globs = _CATEGORY_GLOBS.get(cat, [])
        title = cat.capitalize()
        lines = [
            "---",
            f"description: UK Building Regulations — {title} (current truth, auto-generated)",
        ]
        if globs:
            rendered_globs = ", ".join(f'"{g}"' for g in globs)
            lines.append(f"globs: [{rendered_globs}]")
        else:
            lines.append('globs: []')
        lines.extend([
            "alwaysApply: false",
            "---",
            "",
        ])
        lines.extend(_render_rule_section(cat_rules, title=title))
        out[f".cursor/rules/{cat}.mdc"] = "\n".join(lines)

    # A flat .cursorrules (legacy format) aggregating everything — for editors
    # / assistants that don't yet support the .mdc directory layout.
    flat: list[str] = [
        "# UK Building Regulations — Current Context",
        f"# Auto-generated {datetime.now(UTC).isoformat(timespec='seconds')}",
        "",
    ]
    for cat in sorted(by_cat):
        flat.append(f"## {cat.capitalize()}")
        flat.extend(_render_rule_section(by_cat[cat]))
        flat.append("")
    out[".cursorrules"] = "\n".join(flat)

    return out


# --------------------------------------------------------------------------- #
#  CLAUDE.md — repo-root brief for Claude Code / Claude Desktop
# --------------------------------------------------------------------------- #
def render_claude_md(rules: list[Rule]) -> tuple[str, str]:
    """
    Render a single ``CLAUDE.md`` — the repo-root context file Claude Code
    and Claude Desktop read automatically.

    Dense and citation-bearing: every rule is a one-liner with its parameters
    and source citation, grouped by category, with a "grounding rules" preamble
    instructing the assistant to cite ``rule_id`` + page on every code change.

    Returns ``(filename, content)``.
    """
    by_cat: dict[str, list[Rule]] = defaultdict(list)
    for r in rules:
        by_cat[r.category.value].append(r)

    lines = [
        "# UK Building Regulations — Project Context",
        "",
        f"> Auto-generated by the UK Regs pipeline on "
        f"{datetime.now(UTC).isoformat(timespec='seconds')}.",
        f"> **{len(rules)} active rules** across {len(by_cat)} categories.",
        "> Every rule below is the *current, in-force* truth; superseded "
        "versions are retained in the pipeline but omitted here.",
        "",
        "## How to use this file",
        "",
        "When writing or reviewing code in this repository that touches "
        "building-regulation compliance:",
        "",
        "1. Treat the rules below as authoritative — do not improvise values.",
        "2. For every constant, threshold, or dimension you encode, cite the "
        "``rule_id`` and source page in a code comment, e.g.:",
        "   ```python",
        '   # rule-fire-001: AD-B 2026-ed1 p.42 — FD30 minimum',
        "   FIRE_DOOR_RESISTANCE_MIN = 30  # minutes",
        "   ```",
        "3. If a rule appears missing or contradictory, flag it rather than "
        "guessing — compliance is safety-critical.",
        "",
        "---",
        "",
    ]

    for cat in sorted(by_cat):
        lines.append(f"## {cat.capitalize()}")
        lines.append("")
        for r in by_cat[cat]:
            params = (
                ", ".join(f"`{k}={v}`" for k, v in r.parameters.items())
                if r.parameters
                else None
            )
            cite = r.citation
            head = f"- **[{r.rule_id}]** {r.text}"
            tail_parts = [f"`{cite.doc_id} {cite.version} p.{cite.page}`"]
            if params:
                tail_parts.append(params)
            lines.append(f"{head} _({' · '.join(tail_parts)})_")
        lines.append("")

    return "CLAUDE.md", "\n".join(lines)


# --------------------------------------------------------------------------- #
#  Consolidated Markdown — human-readable full dump
# --------------------------------------------------------------------------- #
def render_consolidated_md(rules: list[Rule]) -> tuple[str, str]:
    """Render the full, human-readable consolidated regulations document."""
    lines = [
        "# UK Building Regulations — Current Context",
        "",
        f"_Auto-generated from the UK Regs pipeline. {len(rules)} active rules._",
        "",
        "---",
        "",
    ]
    for r in sorted(rules, key=lambda x: (x.category.value, x.rule_id)):
        lines.extend(_render_rule_detail(r))
    return "CONSOLIDATED_REGULATIONS.md", "\n".join(lines)


# --------------------------------------------------------------------------- #
#  JSON — machine-readable export
# --------------------------------------------------------------------------- #
def render_json(rules: list[Rule]) -> tuple[str, str]:
    """
    Render a machine-readable JSON export of every current rule.

    Useful for diffing exports across versions, or for tooling that wants the
    structured data without parsing markdown.
    """
    import json

    def _rule_dict(r: Rule) -> dict[str, Any]:
        c = r.citation
        return {
            "rule_id": r.rule_id,
            "doc_id": r.doc_id,
            "version": r.version,
            "section_path": r.section_path,
            "category": r.category.value,
            "title": r.title,
            "text": r.text,
            "normalized_form": r.normalized_form,
            "parameters": r.parameters,
            "references": r.references,
            "valid_from": r.valid_from.isoformat(),
            "valid_to": r.valid_to.isoformat() if r.valid_to else None,
            "status": r.status.value,
            "confidence": r.confidence,
            "extraction_method": r.extraction_method,
            "citation": {
                "doc_id": c.doc_id,
                "version": c.version,
                "page": c.page,
                "raw_text": c.raw_text,
                "url": c.url,
            },
        }

    payload = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "rule_count": len(rules),
        "rules": [_rule_dict(r) for r in rules],
    }
    return "regulations.json", json.dumps(payload, indent=2, ensure_ascii=False)


# --------------------------------------------------------------------------- #
#  Orchestrator — render everything
# --------------------------------------------------------------------------- #
def render_all(rules: list[Rule]) -> dict[str, str]:
    """
    Render the full artifact bundle.

    Returns a mapping of ``relative_path -> file_content`` covering:

    * ``.cursor/rules/<category>.mdc`` (one per category)
    * ``.cursorrules`` (legacy flat file)
    * ``CLAUDE.md``
    * ``CONSOLIDATED_REGULATIONS.md``
    * ``regulations.json``

    Consumers (API, CLI) can iterate this map to write a directory tree,
    serve downloads, or zip the bundle.
    """
    out: dict[str, str] = {}
    out.update(render_cursor_mdc(rules))

    name, content = render_claude_md(rules)
    out[name] = content

    name, content = render_consolidated_md(rules)
    out[name] = content

    name, content = render_json(rules)
    out[name] = content

    return out


# --------------------------------------------------------------------------- #
#  Internal helpers
# --------------------------------------------------------------------------- #
def _render_rule_section(rules: list[Rule], *, title: str | None = None) -> list[str]:
    """Render a list of rules as a markdown section (used by .mdc + .cursorrules)."""
    lines: list[str] = []
    if title:
        lines.append(f"# {title} — Current Rules")
        lines.append("")
    for r in rules:
        lines.extend(_render_rule_detail(r))
    return lines


def _render_rule_detail(r: Rule) -> list[str]:
    """Render a single rule as a rich markdown block with full provenance."""
    lines = [
        f"## {r.title or r.rule_id}",
        "",
        f"- **Rule ID:** `{r.rule_id}`",
        f"- **Section:** `{r.section_path}`",
        f"- **Category:** {r.category.value}",
        f"- **Status:** {r.status.value} (valid from {r.valid_from})",
        "",
        f"> {r.text}",
        "",
    ]
    if r.parameters:
        lines.append("**Parameters:**")
        for k, v in r.parameters.items():
            lines.append(f"- `{k}` = `{v}`")
        lines.append("")
    if r.references:
        lines.append(f"**References:** {', '.join(r.references)}")
        lines.append("")
    c = r.citation
    lines.append(
        f"**Source:** {c.doc_id} {c.version}, p.{c.page} — _\u201c{c.raw_text}\u201d_"
    )
    lines.append("")
    lines.append("---")
    lines.append("")
    return lines