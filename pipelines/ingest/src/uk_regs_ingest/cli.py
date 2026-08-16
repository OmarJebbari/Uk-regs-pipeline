"""
Command-line interface for the ingest pipeline.

Examples
--------
# Full ingest with LLM (requires OPENAI_API_KEY + running services):
uk-regs-ingest ingest path/to/adb-2026.pdf \
    --title "Approved Document B" --version 2026-ed1

# Offline / deterministic-only (no API key needed):
uk-regs-ingest ingest path/to/adb-2026.pdf \
    --title "Approved Document B" --version 2026-ed1 --skip-llm

# Apply the change-set (set rules ACTIVE). Use after review:
uk-regs-ingest ingest ... --apply

# Backfill embeddings skipped during a --skip-embed run:
uk-regs-ingest reembed [--dry-run] [--batch-size 64]
"""

from __future__ import annotations

import logging
import sys
from datetime import date
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table
from uk_regs_core import DocumentType, get_settings
from uk_regs_extraction.llm.client import LLMClient, get_llm_client
from uk_regs_knowledge.embeddings import Embedder, get_embedder

from uk_regs_ingest.pipeline import IngestPipeline, IngestResult

console = Console()


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _build_pipeline(*, skip_llm: bool, skip_embed: bool) -> IngestPipeline:
    """Wire up the pipeline with the appropriate optional components."""
    settings = get_settings()

    llm: LLMClient | None = None
    if not skip_llm:
        try:
            llm = get_llm_client(settings)
        except Exception as e:
            console.print(
                f"[yellow]LLM unavailable ({e}); continuing in deterministic-only mode[/yellow]"
            )
            llm = None

    embedder: Embedder | None = None
    if not skip_embed:
        try:
            embedder = get_embedder(settings)
        except Exception as e:
            console.print(
                f"[yellow]Embedder unavailable ({e}); rules stored without vectors[/yellow]"
            )
            embedder = None

    return IngestPipeline(settings=settings, llm=llm, embedder=embedder)


# --------------------------------------------------------------------------- #
#  Commands
# --------------------------------------------------------------------------- #
@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Debug logging")
def cli(verbose: bool) -> None:
    """UK Building Regulations ingest pipeline."""
    _setup_logging(verbose)


@cli.command("init-db")
@click.option(
    "--schema",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=Path("packages/knowledge/src/uk_regs_knowledge/relational/schema.sql"),
    help="Path to schema.sql",
)
def init_db(schema: Path) -> None:
    """Apply the Postgres + pgvector schema (idempotent)."""
    from uk_regs_knowledge.relational.connection import init_schema

    # schema.sql is packaged as a resource; the --schema flag is kept for
    # backward-compat but the packaged copy is authoritative.
    init_schema(get_settings())
    console.print("[green]✓[/green] schema applied (pgvector + tables)")


@cli.command()
@click.argument("pdf_path", type=click.Path(exists=True, path_type=Path))
@click.option("--title", required=True, help="Document title, e.g. 'Approved Document B'")
@click.option("--version", "version", required=True, help="Version label, e.g. '2026-ed1'")
@click.option(
    "--doc-type",
    type=click.Choice([t.value for t in DocumentType]),
    default=DocumentType.APPROVED_DOCUMENT.value,
    help="Document classification",
)
@click.option("--source-url", default=None, help="Provenance URL")
@click.option("--effective-from", default=None, help="YYYY-MM-DD when rules become effective")
@click.option("--apply", is_flag=True, help="Set rules ACTIVE (default: leave as PROPOSED)")
@click.option("--skip-llm", is_flag=True, help="Deterministic extraction only (offline, cheap)")
@click.option("--skip-embed", is_flag=True, help="Do not generate embeddings")
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="If set, dump extracted rules + change-set as JSON to this dir",
)
def ingest(
    pdf_path: Path,
    title: str,
    version: str,
    doc_type: str,
    source_url: str | None,
    effective_from: str | None,
    apply: bool,
    skip_llm: bool,
    skip_embed: bool,
    out_dir: Path | None,
) -> None:
    """Ingest a single PDF end-to-end."""
    eff = date.fromisoformat(effective_from) if effective_from else None
    pipeline = _build_pipeline(skip_llm=skip_llm, skip_embed=skip_embed)

    console.print(f"[bold cyan]Ingesting[/bold cyan] {pdf_path.name}")
    result = pipeline.run(
        pdf_path,
        title=title,
        version=version,
        doc_type=DocumentType(doc_type),
        source_url=source_url,
        effective_from=eff,
        apply=apply,
        skip_llm=skip_llm,
        out_dir=out_dir,
    )
    _print_result(result)


@cli.command()
@click.argument("directory", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--version", "version", required=True, help="Version label for all PDFs in the batch")
@click.option("--skip-llm", is_flag=True)
@click.option("--skip-embed", is_flag=True)
@click.option("--apply", is_flag=True)
def batch(
    directory: Path,
    version: str,
    skip_llm: bool,
    skip_embed: bool,
    apply: bool,
) -> None:
    """Ingest every PDF in a directory (title = filename stem)."""
    pdfs = sorted(directory.glob("*.pdf"))
    if not pdfs:
        console.print(f"[yellow]No PDFs found in {directory}[/yellow]")
        sys.exit(1)

    pipeline = _build_pipeline(skip_llm=skip_llm, skip_embed=skip_embed)
    results: list[IngestResult] = []
    for pdf in pdfs:
        console.print(f"\n[bold cyan]==[/bold cyan] {pdf.name}")
        try:
            r = pipeline.run(
                pdf,
                title=pdf.stem.replace("_", " ").replace("-", " "),
                version=version,
                apply=apply,
                skip_llm=skip_llm,
            )
            results.append(r)
        except Exception:
            console.print(f"[red]FAILED[/red] {pdf.name}")
            logging.getLogger(__name__).exception("batch ingest failed for %s", pdf)

    _print_batch_summary(results)


@cli.command()
@click.option("--batch-size", default=64, show_default=True, help="Texts per embed API call")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Only report how many rules are missing vectors; change nothing",
)
def reembed(batch_size: int, dry_run: bool) -> None:
    """Backfill embeddings for rules persisted without vectors.

    Rules can be stored without vectors when ingest ran with --skip-embed,
    no embedder was configured, or the embedding API failed mid-run. Those
    rules are invisible to semantic search until backfilled — this command
    embeds them using the same text (normalized_form) the pipeline uses,
    so backfilled vectors match ingest-time vectors.
    """
    from uk_regs_knowledge.repositories.relational_repo import RuleRepository

    repo = RuleRepository()
    missing = repo.count_missing_embeddings()
    if missing == 0:
        console.print("[green]✓[/green] All rules already have embeddings — nothing to do.")
        return
    if dry_run:
        console.print(f"[cyan]{missing}[/cyan] rules missing embeddings (dry run, no changes).")
        return

    try:
        embedder = get_embedder(get_settings())
    except Exception as e:
        console.print(f"[red]Embedder unavailable ({e}); cannot backfill.[/red]")
        sys.exit(1)

    console.print(f"[cyan]{missing}[/cyan] rules to embed (batch size {batch_size})…")
    done = failed = 0
    batch_ids: list[str] = []
    batch_texts: list[str] = []

    def _flush() -> None:
        nonlocal done, failed, batch_ids, batch_texts
        if not batch_ids:
            return
        try:
            vectors = embedder.embed_texts(batch_texts)
            for rid, vec in zip(batch_ids, vectors, strict=False):
                repo.update_embedding(rid, vec)
            done += len(batch_ids)
            console.print(f"  [green]✓[/green] embedded {len(batch_ids)} ({done}/{missing})")
        except Exception:
            failed += len(batch_ids)
            logging.getLogger(__name__).exception(
                "reembed batch failed (%s rules)", len(batch_ids)
            )
        batch_ids, batch_texts = [], []

    for rule_id, normalized_form in repo.iter_missing_embeddings(batch_size=batch_size):
        batch_ids.append(rule_id)
        batch_texts.append(normalized_form or "")
        if len(batch_ids) >= batch_size:
            _flush()
    _flush()

    if failed:
        console.print(
            f"[yellow]Done: {done} embedded, [red]{failed} failed[/red]. "
            "Re-run to retry failures.[/yellow]"
        )
        sys.exit(1)
    console.print(f"[green]✓[/green] Backfilled {done} embeddings.")


# --------------------------------------------------------------------------- #
#  Output
# --------------------------------------------------------------------------- #
def _print_result(r: IngestResult) -> None:
    table = Table(title=f"Ingest summary — {r.doc_id} {r.version}", show_header=False)
    table.add_column("field", style="bold")
    table.add_column("value")
    table.add_row("pages", str(r.pages))
    table.add_row("blocks", str(r.blocks))
    table.add_row("figures", str(r.figures))
    table.add_row("rules extracted", str(r.rules_extracted))
    table.add_row("decisions", ", ".join(f"{k}={v}" for k, v in r.decisions.items()) or "—")
    table.add_row("needs review", str(r.needs_review))
    table.add_row("change_set_id", r.change_set_id or "—")
    table.add_row("applied", "yes" if r.applied else "no (proposed)")
    console.print(table)


def _print_batch_summary(results: list[IngestResult]) -> None:
    table = Table(title="Batch summary")
    table.add_column("doc_id", style="bold")
    table.add_column("rules", justify="right")
    table.add_column("decisions")
    table.add_column("review", justify="right")
    table.add_column("status")
    for r in results:
        table.add_row(
            f"{r.doc_id} {r.version}",
            str(r.rules_extracted),
            ", ".join(f"{k}={v}" for k, v in r.decisions.items()) or "—",
            str(r.needs_review),
            "applied" if r.applied else "proposed",
        )
    console.print(table)


def main() -> None:
    cli()


if __name__ == "__main__":
    main()