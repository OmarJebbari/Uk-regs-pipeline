"""
Command-line interface for the evaluation harness.

Runs the golden-set suites (retrieval, reconciliation, extraction, llm_judge)
and prints a Markdown report. Exits non-zero when overall accuracy falls below
the gate (default ``1.0`` — every golden case must pass for a compliance pipeline).

Examples
--------
# Run all suites with the mock backend (no DB / no LLM required):
uk-regs-eval run

# Run only extraction, allow 90% accuracy:
uk-regs-eval run --suite extraction --gate 0.9

# Run only the LLM-as-a-judge context-quality suite:
uk-regs-eval run --suite llm_judge

# Skip the live-DB retrieval pass (useful in CI without a populated index):
uk-regs-eval run --no-retrieval

# Persist the report to disk:
uk-regs-eval run --out reports/eval.md
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from uk_regs_evals.harnesses import (
    run_all,
)

console = Console()


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _default_backend():
    """Resolve the search backend (mock unless a real DB is configured)."""
    from uk_regs_api.service import get_backend

    return get_backend()


# --------------------------------------------------------------------------- #
#  Commands
# --------------------------------------------------------------------------- #
@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Debug logging")
def cli(verbose: bool) -> None:
    """UK Regs Pipeline — evaluation harness (golden-set regression suite)."""
    _setup_logging(verbose)


@cli.command("run")
@click.option(
    "--suite",
    type=click.Choice(["all", "retrieval", "reconciliation", "extraction", "llm_judge"]),
    default="all",
    help="Which suite to run (default: all)",
)
@click.option(
    "--gate",
    type=float,
    default=1.0,
    help="Minimum overall accuracy to exit 0 (default 1.0 — every case must pass)",
)
@click.option("--no-retrieval", is_flag=True, help="Skip the retrieval suite")
@click.option("--no-reconciliation", is_flag=True, help="Skip the reconciliation suite")
@click.option("--no-extraction", is_flag=True, help="Skip the extraction suite")
@click.option("--no-llm-judge", is_flag=True, help="Skip the LLM-judge context-quality suite")
@click.option(
    "--out",
    "out_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Write the Markdown report to this file",
)
@click.option("--no-color", is_flag=True, help="Plain output (no ANSI colours)")
def run(
    suite: str,
    gate: float,
    no_retrieval: bool,
    no_reconciliation: bool,
    no_extraction: bool,
    no_llm_judge: bool,
    out_path: Path | None,
    no_color: bool,
) -> None:
    """Run the golden-set eval suites and print a report."""
    # Map --suite to the run_* flags. ``--no-*`` always wins (most restrictive).
    run_ret = run_rec = run_ext = run_judge = True
    if suite == "retrieval":
        run_rec = run_ext = run_judge = False
    elif suite == "reconciliation":
        run_ret = run_ext = run_judge = False
    elif suite == "extraction":
        run_ret = run_rec = run_judge = False
    elif suite == "llm_judge":
        run_ret = run_rec = run_ext = False
    if no_retrieval:
        run_ret = False
    if no_reconciliation:
        run_rec = False
    if no_extraction:
        run_ext = False
    if no_llm_judge:
        run_judge = False

    backend = None
    if run_ret:
        try:
            backend = _default_backend()
        except Exception as e:  # pragma: no cover - environment dependent
            console.print(f"[yellow]Search backend unavailable ({e}); skipping retrieval[/yellow]")
            run_ret = False

    report = run_all(
        backend=backend,
        run_retrieval=run_ret,
        run_reconciliation=run_rec,
        run_extraction=run_ext,
        run_llm_judge=run_judge,
    )

    md = report.to_markdown()
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(md, encoding="utf-8")
        console.print(f"[green]✓[/green] report written to {out_path}")

    # Rich rendering of the summary table.
    _print_report(report, use_color=not no_color)

    passed = report.passes_gate(gate)
    label = "PASS" if passed else "FAIL"
    colour = "green" if passed else "red"
    console.print(
        Panel(
            f"[bold {colour}]{label}[/bold {colour}]  "
            f"overall accuracy {report.overall_accuracy:.1%} "
            f"(gate {gate:.0%})",
            title="Eval result",
            border_style=colour,
        )
    )

    sys.exit(0 if passed else 1)


@cli.command("list")
def list_cases() -> None:
    """List the golden cases in each suite (no execution)."""
    from uk_regs_evals.golden import load_cases

    table = Table(title="Golden cases", show_lines=True)
    table.add_column("Suite", style="bold cyan")
    table.add_column("ID", style="bold")
    table.add_column("Summary", overflow="fold")

    for suite in ("retrieval", "reconciliation", "extraction", "llm_judge"):
        try:
            cases = load_cases(suite)
        except FileNotFoundError as e:
            table.add_row(suite, "-", f"[red]{e}[/red]")
            continue
        for c in cases:
            if suite == "retrieval":
                summary = f"q={c.query!r} → {c.expected_rule_ids}"
            elif suite == "reconciliation":
                summary = f"{c.description} (expect {c.expected_decision})"
            elif suite == "llm_judge":
                summary = f"q={c.query!r} → {c.expected_rule_ids}"
            else:
                summary = c.text[:80] + ("…" if len(c.text) > 80 else "")
            table.add_row(suite, c.id, summary)

    console.print(table)


# --------------------------------------------------------------------------- #
#  Rendering
# --------------------------------------------------------------------------- #
def _print_report(report, *, use_color: bool = True) -> None:
    """Render the EvalReport summary as a Rich table + Markdown failures."""
    md_kwargs = {} if use_color else {"code_theme": "ansi_dark"}
    table = Table(title="Eval summary", show_header=True, header_style="bold magenta")
    table.add_column("Suite")
    table.add_column("Total", justify="right")
    table.add_column("Passed", justify="right")
    table.add_column("Accuracy", justify="right")
    table.add_column("Mean score", justify="right")

    for r in report.all_reports():
        acc = f"{r.accuracy:.1%}"
        if use_color:
            acc = f"[green]{acc}[/green]" if r.accuracy >= 1.0 else f"[red]{acc}[/red]"
        table.add_row(
            r.suite,
            str(r.total),
            str(r.passed),
            acc,
            f"{r.mean_score:.3f}",
        )
    table.add_row(
        "[bold]OVERALL[/bold]",
        str(sum(r.total for r in report.all_reports())),
        str(sum(r.passed for r in report.all_reports())),
        f"[bold]{report.overall_accuracy:.1%}[/bold]",
        "",
    )
    console.print(table)

    # Failures detail.
    failures_md = []
    for r in report.all_reports():
        bad = [c for c in r.results if not c.passed]
        if bad:
            failures_md.append(f"### {r.suite} — {len(bad)} failure(s)")
            for c in bad:
                failures_md.append(f"- `{c.case_id}` — {c.detail}")
    if failures_md:
        console.print(Markdown("\n\n".join(failures_md), **md_kwargs))


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
