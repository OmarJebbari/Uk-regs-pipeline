"""
CLI entrypoint for the sourcing package.

Usage:
    # One-shot poll (for cron / GitHub Actions)
    python -m uk_regs_sourcing --once --registry sourcing/sources.yaml

    # Long-running scheduler
    python -m uk_regs_sourcing --interval 60

    # Force re-ingest everything
    python -m uk_regs_sourcing --once --force --apply
"""

from __future__ import annotations

import argparse
import logging
import sys


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="uk-regs-sourcing",
        description="Watch regulatory sources and auto-ingest new/changed PDFs.",
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--once",
        action="store_true",
        help="Run a single poll cycle and exit (cron/GHA mode).",
    )
    mode.add_argument(
        "--interval",
        type=int,
        metavar="MINUTES",
        help="Run forever, polling every MINUTES (long-running mode).",
    )
    p.add_argument(
        "--registry",
        type=str,
        default=None,
        help="Path to sources.yaml (default: $SOURCES_YAML or ./sourcing/sources.yaml).",
    )
    p.add_argument("--force", action="store_true", help="Ignore poll intervals; re-poll now.")
    p.add_argument(
        "--apply",
        action="store_true",
        help="Auto-apply change-sets (default: leave as 'proposed' for review).",
    )
    p.add_argument(
        "--skip-llm",
        action="store_true",
        help="Deterministic-only extraction (no LLM calls).",
    )
    p.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="-v INFO, -vv DEBUG.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    level = logging.WARNING - 10 * args.verbose
    logging.basicConfig(
        level=max(level, logging.DEBUG),
        format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
    )

    from uk_regs_sourcing.scheduler import Scheduler, run_once

    if args.once:
        run_once(
            registry_path=args.registry,
            force=args.force,
            apply=args.apply,
            skip_llm=args.skip_llm,
        )
        return 0

    if args.interval is not None:
        sched = Scheduler(
            interval_minutes=args.interval,
            force=args.force,
            apply=args.apply,
            skip_llm=args.skip_llm,
            registry_path=args.registry,
        )
        sched.run_forever()
        return 0

    # Default: --once semantics (single cycle)
    run_once(
        registry_path=args.registry,
        force=args.force,
        apply=args.apply,
        skip_llm=args.skip_llm,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
