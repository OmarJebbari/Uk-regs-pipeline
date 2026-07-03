"""
FastAPI app — the context-serving API (Phase 3).

Endpoints:
  GET /health
  GET /search?q=...&top_k=10
  GET /rules/{rule_id}
  GET /current?topic=&category=
  GET /figures/{rule_id}
  GET /diff/{rule_id}
  GET /changes?status=&limit=
  GET /export/markdown        -> consolidated .cursor/rules + CLAUDE.md export

Every response carries citations. The app runs with a Mock backend out of the
box, and auto-upgrades to the Real (Postgres) backend when the DB is reachable.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Query

from uk_regs_api.service import (
    RuleHit,
    changesets_to_dicts,
    get_backend,
    rules_to_dicts,
)

app = FastAPI(
    title="UK Regs Context API",
    description=(
        "Semantic search + current-rule + diff + figures + changelog for UK "
        "building regulations. Every response carries citations."
    ),
    version="0.3.0",
)


# --------------------------------------------------------------------------- #
#  Health
# --------------------------------------------------------------------------- #
@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
#  Search
# --------------------------------------------------------------------------- #
@app.get("/search")
def search(
    q: str = Query(..., description="Natural-language query, e.g. 'fire door rating'"),
    top_k: int = Query(10, ge=1, le=50),
) -> dict:
    """Semantic search over current active rules. Falls back to fulltext."""
    backend = get_backend()
    hits: list[RuleHit] = backend.search(q, top_k=top_k)
    return {
        "query": q,
        "count": len(hits),
        "hits": [h.to_dict() for h in hits],
    }


# --------------------------------------------------------------------------- #
#  Single rule
# --------------------------------------------------------------------------- #
@app.get("/rules/{rule_id}")
def get_rule(rule_id: str) -> dict:
    backend = get_backend()
    rule = backend.get_rule(rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail=f"rule '{rule_id}' not found")
    return rules_to_dicts([rule])[0]


# --------------------------------------------------------------------------- #
#  Current rules (the 'current truth')
# --------------------------------------------------------------------------- #
@app.get("/current")
def get_current(
    topic: str | None = Query(None, description="Free-text topic filter"),
    category: str | None = Query(
        None, description="One of: fire, structure, ventilation, access, "
                          "drainage, energy, sound, safety, other"
    ),
) -> dict:
    backend = get_backend()
    rules = backend.get_current(topic=topic, category=category)
    return {
        "topic": topic,
        "category": category,
        "count": len(rules),
        "rules": rules_to_dicts(rules),
    }


# --------------------------------------------------------------------------- #
#  Figures
# --------------------------------------------------------------------------- #
@app.get("/figures/{rule_id}")
def get_figure(rule_id: str) -> dict:
    backend = get_backend()
    fig = backend.get_figure(rule_id)
    if fig is None:
        return {
            "rule_id": rule_id,
            "figure": None,
            "note": "Figure retrieval lands in Phase 4 (VLM + object store).",
        }
    return {"rule_id": rule_id, "figure": fig}


# --------------------------------------------------------------------------- #
#  Diff (what changed for this rule)
# --------------------------------------------------------------------------- #
@app.get("/diff/{rule_id}")
def diff(rule_id: str) -> dict:
    backend = get_backend()
    return backend.diff(rule_id)


# --------------------------------------------------------------------------- #
#  Change log
# --------------------------------------------------------------------------- #
@app.get("/changes")
def list_changes(
    status: str | None = Query(None, description="proposed|validated|applied|rejected|rolled_back"),
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    backend = get_backend()
    css = backend.list_changes(status=status, limit=limit)
    return {"count": len(css), "change_sets": changesets_to_dicts(css)}


# --------------------------------------------------------------------------- #
#  Export — consolidated Markdown for IDE rules (Cursor/Claude/Copilot)
# --------------------------------------------------------------------------- #
@app.get("/export/markdown")
def export_markdown() -> dict:
    """Generate .cursor/rules + CLAUDE.md content from the current truth."""
    backend = get_backend()
    rules = backend.get_current()
    lines = [
        "# UK Building Regulations — Current Context",
        "",
        f"_Auto-generated from the UK Regs pipeline. {len(rules)} active rules._",
        "",
        "---",
        "",
    ]
    for r in rules:
        lines.append(f"## {r.title or r.rule_id}")
        lines.append(f"- **Rule ID:** `{r.rule_id}`")
        lines.append(f"- **Section:** `{r.section_path}`")
        lines.append(f"- **Category:** {r.category.value}")
        lines.append(f"- **Status:** {r.status.value} (valid from {r.valid_from})")
        lines.append("")
        lines.append(f"> {r.text}")
        lines.append("")
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
    return {
        "filename": "CONSOLIDATED_REGULATIONS.md",
        "content": "\n".join(lines),
        "rule_count": len(rules),
    }