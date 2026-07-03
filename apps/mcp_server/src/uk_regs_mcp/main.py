"""
MCP (Model Context Protocol) server for the UK Regs pipeline.

Exposes the context API as tools that an IDE assistant (Cursor, Claude Desktop,
Copilot) can call live to reason about current UK building regulations.

Tools provided:
  - search_regulations(query, top_k)
  - get_rule(rule_id)
  - get_current(topic?, category?)
  - diff_rule(rule_id)
  - list_changes(status?, limit?)
  - export_markdown()
  - check_design_intent(intent)

Every tool result carries citations so the assistant always grounds its claims.

Transport: stdio (default for local IDE integration). The HTTP transport is
trivial to add; stdio is what Cursor/Claude Desktop expect for local servers.

Usage in Cursor:  add to ~/.cursor/mcp.json:
    {
      "mcpServers": {
        "uk-regs": {
          "command": "uv",
          "args": ["--directory", "<path>/uk-regs-pipeline",
                   "run", "uk-regs-mcp"]
        }
      }
    }
"""

from __future__ import annotations

import json
import os
from typing import Any

# MCP Python SDK — import lazily so the package imports without it installed.
# Falling back to a no-op keeps the rest of the codebase import-safe in CI.
try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # pragma: no cover
    FastMCP = None  # type: ignore[assignment,misc]

from uk_regs_api.service import get_backend


# --------------------------------------------------------------------------- #
#  API client — talk to the FastAPI service when it's up; else call the
#  backend directly (in-process) so the MCP server works standalone too.
# --------------------------------------------------------------------------- #
def _call(method: str, path: str, **params: Any) -> Any:
    """
    Prefer calling the live API over HTTP (decoupled, production-like).
    Fall back to calling the backend in-process if the API isn't running
    or httpx isn't available — so `uk-regs-mcp` always works standalone.
    """
    base = os.environ.get("UK_REGS_API_URL", "").rstrip("/")
    if base:
        try:
            import httpx

            with httpx.Client(base_url=base, timeout=10.0) as client:
                resp = client.request(method, path, params={k: v for k, v in params.items() if v is not None})
                resp.raise_for_status()
                return resp.json()
        except Exception:
            pass  # fall through to in-process

    # In-process fallback (uses the same backend selection as the API)
    backend = get_backend()
    if path.startswith("/search"):
        return {"query": params.get("q", ""), "hits": [h.to_dict() for h in backend.search(params.get("q", ""), top_k=int(params.get("top_k", 10)))]}
    if path.startswith("/current"):
        rules = backend.get_current(topic=params.get("topic"), category=params.get("category"))
        return {"rules": [_r_to_dict(r) for r in rules]}
    if path.startswith("/changes"):
        return {"change_sets": [_cs_to_dict(c) for c in backend.list_changes(status=params.get("status"), limit=int(params.get("limit", 50)))]}
    if path.startswith("/export/markdown"):
        return backend_diff_export(backend)
    return {"error": f"unsupported path {path}"}


def backend_diff_export(backend) -> dict:
    rules = backend.get_current()
    return {
        "filename": "CONSOLIDATED_REGULATIONS.md",
        "rule_count": len(rules),
        "rules": [_r_to_dict(r) for r in rules],
    }


# --------------------------------------------------------------------------- #
#  Serialisers (kept here so the MCP server has zero hard dep on FastAPI app)
# --------------------------------------------------------------------------- #
def _r_to_dict(r) -> dict:
    return {
        "rule_id": r.rule_id,
        "doc_id": r.doc_id,
        "version": r.version,
        "section_path": r.section_path,
        "category": r.category.value,
        "title": r.title,
        "text": r.text,
        "parameters": r.parameters,
        "references": r.references,
        "valid_from": r.valid_from.isoformat(),
        "valid_to": r.valid_to.isoformat() if r.valid_to else None,
        "status": r.status.value,
        "confidence": r.confidence,
        "citation": {
            "doc_id": r.citation.doc_id,
            "version": r.citation.version,
            "page": r.citation.page,
            "raw_text": r.citation.raw_text,
        },
    }


def _cs_to_dict(cs) -> dict:
    return {
        "change_set_id": cs.change_set_id,
        "doc_id": cs.doc_id,
        "version": cs.version,
        "status": cs.status,
        "delta_count": len(cs.deltas),
        "deltas": [
            {
                "new_rule_id": d.new_rule_id,
                "matched_rule_id": d.matched_rule_id,
                "decision": d.decision.value,
                "confidence": d.confidence,
                "rationale": d.rationale,
                "diff_summary": d.diff_summary,
            }
            for d in cs.deltas
        ],
    }


# --------------------------------------------------------------------------- #
#  Server definition
# --------------------------------------------------------------------------- #
def build_server():  # type: ignore[no-untyped-def]
    """Build and return the FastMCP server (requires `mcp` installed)."""
    if FastMCP is None:
        raise RuntimeError(
            "MCP SDK not installed. Run: uv pip install mcp  (or pip install mcp)"
        )

    server = FastMCP("uk-regs")

    @server.tool()
    def search_regulations(query: str, top_k: int = 10) -> str:
        """Search current UK building regulations by natural-language query.

        Returns ranked rule hits, each with full text + citation (doc, version,
        page, raw source snippet). Use this when you need rules relevant to a
        design intent or code change.
        """
        res = _call("GET", "/search", q=query, top_k=top_k)
        hits = res.get("hits", [])
        if not hits:
            return f"No regulations matched '{query}'."
        out = [f"# Top {len(hits)} regulation(s) for '{query}'\n"]
        for i, h in enumerate(hits, 1):
            r = h.get("rule", {})
            c = h.get("citation", {})
            out.append(
                f"## {i}. {r.get('title', r.get('rule_id'))}  (score {h.get('score', 0):.2f})\n"
                f"- **Rule ID:** {r.get('rule_id')}\n"
                f"- **Section:** {r.get('section_path')}\n"
                f"- **Text:** {r.get('text')}\n"
                f"- **Parameters:** `{json.dumps(r.get('parameters', {}))}`\n"
                f"- **Citation:** {c.get('doc_id')} {c.get('version')} p.{c.get('page')} — _{c.get('raw_text')}_\n"
            )
        return "\n".join(out)

    @server.tool()
    def get_rule(rule_id: str) -> str:
        """Fetch a single rule by its stable rule_id, with full citation."""
        backend = get_backend()
        r = backend.get_rule(rule_id)
        if not r:
            return f"Rule '{rule_id}' not found."
        d = _r_to_dict(r)
        return (
            f"# {d['title']}\n"
            f"- **Rule ID:** {d['rule_id']}\n"
            f"- **Section:** {d['section_path']}\n"
            f"- **Text:** {d['text']}\n"
            f"- **Parameters:** `{json.dumps(d['parameters'])}`\n"
            f"- **Status:** {d['status']} (valid from {d['valid_from']})\n"
            f"- **Citation:** {d['citation']['doc_id']} {d['citation']['version']} "
            f"p.{d['citation']['page']} — _{d['citation']['raw_text']}_\n"
        )

    @server.tool()
    def get_current(topic: str | None = None, category: str | None = None) -> str:
        """Return all current (active, in-force) rules, optionally filtered.

        Args:
            topic: free-text filter (matched against title/text)
            category: one of fire|structure|ventilation|access|drainage|energy|sound|safety|other
        """
        res = _call("GET", "/current", topic=topic, category=category)
        rules = res.get("rules", [])
        if not rules:
            return "No current rules match the filters."
        out = [f"# {len(rules)} current rule(s)" +
               (f" for topic '{topic}'" if topic else "") +
               (f" in category '{category}'" if category else "") + "\n"]
        for r in rules:
            out.append(
                f"- **{r.get('title', r.get('rule_id'))}** (`{r.get('rule_id')}`): "
                f"{r.get('text')}  _[{r.get('citation', {}).get('doc_id')} "
                f"{r.get('citation', {}).get('version')} p.{r.get('citation', {}).get('page')}]_"
            )
        return "\n".join(out)

    @server.tool()
    def diff_rule(rule_id: str) -> str:
        """Show what changed for a rule versus its previous version."""
        backend = get_backend()
        d = backend.diff(rule_id)
        if "error" in d:
            return f"Rule '{rule_id}' not found."
        cur = d.get("current", {})
        prev = d.get("previous")
        out = [f"# Diff for {rule_id}\n", "**Current:**", json.dumps(cur, indent=2, default=str)]
        if prev:
            out += ["\n**Previous:**", json.dumps(prev, indent=2, default=str)]
        if d.get("change"):
            out.append(f"\n**Change:** {d['change']}")
        return "\n".join(out)

    @server.tool()
    def list_changes(status: str | None = None, limit: int = 20) -> str:
        """List recent change-sets (corpus updates from new PDFs).

        Args:
            status: filter by proposed|validated|applied|rejected|rolled_back
            limit: max number of change-sets
        """
        res = _call("GET", "/changes", status=status, limit=limit)
        css = res.get("change_sets", [])
        if not css:
            return "No change-sets found."
        out = [f"# {len(css)} change-set(s)\n"]
        for cs in css:
            out.append(
                f"## {cs.get('change_set_id')} ({cs.get('status')})\n"
                f"- doc: {cs.get('doc_id')} {cs.get('version')}\n"
                f"- deltas: {cs.get('delta_count')}"
            )
            for d in cs.get("deltas", []):
                out.append(
                    f"  - [{d.get('decision')}] `{d.get('new_rule_id')}` "
                    f"(conf {d.get('confidence'):.2f}): {d.get('diff_summary') or d.get('rationale')}"
                )
        return "\n".join(out)

    @server.tool()
    def export_markdown() -> str:
        """Export the current consolidated regulations as Markdown
        (suitable for .cursor/rules/*.mdc or CLAUDE.md)."""
        backend = get_backend()
        rules = backend.get_current()
        lines = [
            "# UK Building Regulations — Current Context",
            "",
            f"_Auto-generated by the UK Regs MCP server. {len(rules)} active rules._",
            "",
        ]
        for r in rules:
            lines.append(f"## {r.title or r.rule_id}")
            lines.append(f"- **Rule ID:** `{r.rule_id}`  **Section:** `{r.section_path}`")
            lines.append(f"- {r.text}")
            if r.parameters:
                lines.append(f"- Parameters: `{json.dumps(r.parameters)}`")
            c = r.citation
            lines.append(f"- Source: {c.doc_id} {c.version} p.{c.page}")
            lines.append("")
        return "\n".join(lines)

    @server.tool()
    def check_design_intent(intent: str) -> str:
        """Check a proposed design intent against the current regulations.

        Use this BEFORE writing compliance code: it searches relevant rules and
        reports any that apply, with citations, so the code can be grounded.
        """
        res = _call("GET", "/search", q=intent, top_k=5)
        hits = res.get("hits", [])
        if not hits:
            return f"No regulations found relevant to intent: '{intent}'."
        out = [f"# Design-intent check: '{intent}'\n",
               f"_{len(hits)} relevant regulation(s). Cite these when implementing._\n"]
        for i, h in enumerate(hits, 1):
            r = h.get("rule", {})
            c = h.get("citation", {})
            out.append(
                f"## {i}. {r.get('title', r.get('rule_id'))}\n"
                f"- **Text:** {r.get('text')}\n"
                f"- **Parameters:** `{json.dumps(r.get('parameters', {}))}`\n"
                f"- **Citation:** {c.get('doc_id')} {c.get('version')} p.{c.get('page')}\n"
            )
        out.append("\n_Match every applicable parameter in code; attach the rule_id + citation in comments._")
        return "\n".join(out)

    return server


# --------------------------------------------------------------------------- #
#  Entrypoint
# --------------------------------------------------------------------------- #
def main() -> None:
    server = build_server()
    server.run(transport="stdio")


if __name__ == "__main__":
    main()