"""Tests for structured logging: JSON mode renders parseable JSON lines."""

from __future__ import annotations

import json
import logging

import pytest


@pytest.mark.parametrize("log_format", ["json", "console"])
def test_configure_logging_renderers(log_format, monkeypatch, capsys):
    """setup_logging() wires structlog with the configured renderer."""
    import structlog

    import uk_regs_core.config as cfg_module
    from uk_regs_api.observability import setup_logging

    monkeypatch.setenv("LOG_FORMAT", log_format)
    cfg_module.get_settings.cache_clear()

    setup_logging()  # idempotent — safe to re-run per test

    capsys.readouterr()  # discard anything logged during setup
    structlog.get_logger("test").info("hello", ping="pong")
    out = capsys.readouterr().out

    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert lines, "expected at least one rendered log line"
    line = lines[-1]

    if log_format == "json":
        parsed = json.loads(line)  # must be valid JSON
        assert parsed.get("event") == "hello"
        assert parsed.get("ping") == "pong"
    else:
        assert "hello" in line  # console renderer keeps the event visible


def test_log_format_env_mapping(monkeypatch):
    """Settings expose LOG_FORMAT from UK_REGS_LOG_FORMAT env var."""
    import uk_regs_core.config as cfg_module

    monkeypatch.setenv("LOG_FORMAT", "json")
    cfg_module.get_settings.cache_clear()
    settings = cfg_module.get_settings()
    assert settings.log_format == "json"