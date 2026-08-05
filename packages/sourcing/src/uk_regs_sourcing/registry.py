"""
Registry loader — reads sources.yaml into a typed SourceRegistry.

The YAML schema is a list of sources under the top-level ``sources:`` key:

    sources:
      - source_id: gov-uk-adb
        title: Approved Document B
        fetcher: http
        url: https://assets.publishing.service.gov.uk/.../ad-b-manifest.json
        poll_interval_minutes: 1440
      - source_id: local-fixtures
        title: Local test fixtures
        fetcher: local
        path: ./fixtures
        enabled: false
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from uk_regs_sourcing.models import SourceRegistry

logger = logging.getLogger(__name__)


def load_registry(path: str | Path | None = None) -> SourceRegistry:
    """
    Load the source registry from a YAML file.

    Args:
        path: explicit path to sources.yaml. If None, looks for, in order:
              $SOURCES_YAML env var, ./sourcing/sources.yaml, ./sources.yaml.
    """
    import os

    if path is None:
        path = os.environ.get("SOURCES_YAML")
    if path is None:
        # Anchor to project root so discovery works regardless of cwd.
        # registry.py → uk_regs_sourcing → src → sourcing → packages → <root>
        _project_root = Path(__file__).resolve().parents[4]
        for candidate in (
            _project_root / "sources.yaml",
            Path("sourcing/sources.yaml"),
            Path("sources.yaml"),
        ):
            if candidate.exists():
                path = candidate
                break
    if path is None:
        logger.info("no sources.yaml found; source registry is empty")
        return SourceRegistry(sources=[])

    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"sources.yaml not found at {p}")

    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{p}: expected a YAML mapping, got {type(raw).__name__}")

    sources = raw.get("sources", [])
    if not isinstance(sources, list):
        raise ValueError(f"{p}: 'sources' must be a list")

    registry = SourceRegistry.model_validate({"sources": sources})
    logger.info(
        "loaded %d source(s) from %s (%d enabled)",
        len(registry.sources),
        p,
        len(registry.enabled_sources()),
    )
    return registry
