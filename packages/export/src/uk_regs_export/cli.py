"""
CLI for the static context exporter.

Usage examples
--------------
    # Render the full bundle from the running API, write to dist/export/
    uk-regs-export render --api http://localhost:8000 --out dist/export/

    # Re-render from a previously-exported regulations.json
    uk-regs-export render --json regulations.json --out ./

    # List what would be generated (dry run)
    uk-regs-export render --api http://localhost:8000 --dry-run

Architecture note
-----------------
The export package has **zero dependency on the API package** — the renderers
are pure functions. This CLI fetches rules over HTTP from the running API (or
from a local JSON file), then calls the pure renderers. This keeps the
dependency graph acyclic: api → export, never export → api.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from uk_regs_core import (
    Category,
    Citation,
    Rule,
    RuleStatus,
)
from uk_regs_core.enums import DocumentType  # noqa: F401  (kept for future filters)

from uk_regs_export.renderers import render_all

app = typer.Typer(
    name="uk-regs-export",
    help="Render current UK Building Regulations into IDE-native context files.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def _main() -> None:
    """UK Regs Export — render consolidated regulations into IDE context files.

    The callback exists so that ``render`` is treated as a named subcommand
    (Typer collapses single-command apps otherwise) and so the CLI is ready
    for additional subcommands in the future.
    """


def _fetch_from_api(base_url: str) -> list[Rule]:
    """Fetch current rules from the running context API over HTTP."""
    import urllib.request

    url = base_url.rstrip("/") + "/current"
    typer.echo(f"  → fetching rules from {url} ...", err=True)
    with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310
        payload = json.loads(resp.read())

    rules_data = payload.get("rules", []) if isinstance(payload, dict) else payload
    return [_dict_to_rule(r) for r in rules_data]


def _load_from_json(path: Path) -> list[Rule]:
    """Load rules from a previously-exported regulations.json."""
    typer.echo(f"  → loading rules from {path} ...", err=True)
    data = json.loads(path.read_text(encoding="utf-8"))
    rules_data = data.get("rules", data) if isinstance(data, dict) else data
    return [_dict_to_rule(r) for r in rules_data]


def _dict_to_rule(d: dict) -> Rule:
    """Reconstruct a Rule model from a JSON dict (API / file round-trip)."""
    cite_d = d.get("citation", {})
    return Rule(
        rule_id=d["rule_id"],
        doc_id=d["doc_id"],
        version=d["version"],
        section_path=d.get("section_path", ""),
        category=Category(d.get("category", "other")),
        title=d.get("title"),
        text=d["text"],
        normalized_form=d.get("normalized_form", d["text"]),
        parameters=d.get("parameters", {}),
        references=d.get("references", []),
        valid_from=d["valid_from"],
        valid_to=d.get("valid_to"),
        recorded_at=d.get("recorded_at", "2025-01-01T00:00:00Z"),
        status=RuleStatus(d.get("status", "active")),
        citation=Citation(
            doc_id=cite_d.get("doc_id", d["doc_id"]),
            version=cite_d.get("version", d["version"]),
            page=cite_d.get("page", 1),
            raw_text=cite_d.get("raw_text", d["text"]),
            url=cite_d.get("url"),
        ),
        confidence=d.get("confidence", 1.0),
        extraction_method=d.get("extraction_method", "llm"),
    )


@app.command()
def render(
    api: Annotated[
        str | None,
        typer.Option(
            "--api", help="Base URL of the running context API (e.g. http://localhost:8000)"
        ),
    ] = None,
    json_file: Annotated[
        Path | None,
        typer.Option("--json", help="Path to a regulations.json to re-render"),
    ] = None,
    out: Annotated[
        Path,
        typer.Option("--out", "-o", help="Output directory"),
    ] = Path("dist/export"),
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="List what would be generated without writing"),
    ] = False,
) -> None:
    """Render Cursor .mdc, CLAUDE.md, consolidated markdown, and JSON."""
    # ---- source the rules ----
    if json_file:
        rules = _load_from_json(json_file)
    elif api:
        rules = _fetch_from_api(api)
    else:
        typer.secho(
            "Error: provide --api <url> or --json <path> to source the rules.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1)

    if not rules:
        typer.secho("Warning: no rules found — nothing to export.", fg=typer.colors.YELLOW)
        raise typer.Exit(0)

    typer.echo(f"  → {len(rules)} rules loaded.", err=True)

    # ---- render ----
    bundle = render_all(rules)

    if dry_run:
        typer.echo("\nDry run — would generate:")
        for path in sorted(bundle):
            typer.echo(f"  {out}/{path}  ({len(bundle[path]):,} bytes)")
        raise typer.Exit(0)

    # ---- write ----
    out.mkdir(parents=True, exist_ok=True)
    for rel_path, content in bundle.items():
        target = out / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        typer.echo(f"  ✓ {target}  ({len(content):,} bytes)")

    typer.secho(f"\n✅ Exported {len(bundle)} files to {out}/", fg=typer.colors.GREEN)
    typer.echo(
        "\nNext steps:\n"
        "  • Copy .cursor/rules/ into your project root for Cursor IDE.\n"
        "  • Copy CLAUDE.md to your project root for Claude Code.\n"
        "  • Commit the bundle so your team shares the same regulatory context."
    )


if __name__ == "__main__":
    app()
