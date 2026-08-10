"""
Tests for the Phase 6-1 static context export package.

Three layers:
  1. Pure renderers  — ``render_cursor_mdc``, ``render_claude_md``,
     ``render_consolidated_md``, ``render_json``, ``render_all``.
  2. API endpoints   — ``/export/bundle``, ``/export/claude.md``,
     ``/export/cursor/{cat}.mdc``, ``/export/regulations.json``.
  3. CLI             — ``uk-regs-export render --json ... --dry-run``.
"""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from uk_regs_core import Category, Citation, Rule, RuleStatus
from uk_regs_export.renderers import (
    render_all,
    render_claude_md,
    render_consolidated_md,
    render_cursor_mdc,
    render_json,
)


# --------------------------------------------------------------------------- #
#  Fixtures
# --------------------------------------------------------------------------- #
def _make_rule(
    *,
    rule_id: str = "rule-fire-001",
    text: str = "Fire doors must provide at least 30 minutes of resistance.",
    category: Category = Category.FIRE,
    page: int = 42,
    parameters: dict | None = None,
    references: list[str] | None = None,
) -> Rule:
    return Rule(
        rule_id=rule_id,
        doc_id="AD-B",
        version="2026-ed1",
        section_path="AD-B/v2/2/2.3/b",
        category=category,
        title="Fire door resistance",
        text=text,
        normalized_form=text.lower(),
        parameters=parameters or {"min_resistance_min": 30},
        references=references or ["BS 476-22"],
        valid_from=date(2026, 1, 1),
        valid_to=None,
        recorded_at=datetime(2026, 1, 1, 0, 0, 0),
        status=RuleStatus.ACTIVE,
        citation=Citation(
            doc_id="AD-B",
            version="2026-ed1",
            page=page,
            raw_text=text,
            url="https://example.gov.uk/ad-b.pdf",
        ),
        confidence=0.95,
        extraction_method="llm",
    )


@pytest.fixture
def sample_rules() -> list[Rule]:
    """A small, multi-category fixture set."""
    return [
        _make_rule(
            rule_id="rule-fire-001",
            text="Fire doors must provide at least 30 minutes of resistance.",
            category=Category.FIRE,
            parameters={"min_resistance_min": 30},
        ),
        _make_rule(
            rule_id="rule-struct-001",
            text="Minimum floor live load is 1.5 kN/m².",
            category=Category.STRUCTURE,
            page=12,
            parameters={"min_live_load_kn_m2": 1.5},
        ),
        _make_rule(
            rule_id="rule-vent-001",
            text="Habitable rooms require 0.5 air changes per hour.",
            category=Category.VENTILATION,
            page=7,
            parameters={"min_ach": 0.5},
        ),
    ]


@pytest.fixture
def single_rule() -> list[Rule]:
    return [_make_rule()]


# --------------------------------------------------------------------------- #
#  1. Pure renderers
# --------------------------------------------------------------------------- #
class TestRenderCursorMdc:
    def test_emits_one_file_per_category(self, sample_rules):
        bundle = render_cursor_mdc(sample_rules)
        assert ".cursor/rules/fire.mdc" in bundle
        assert ".cursor/rules/structure.mdc" in bundle
        assert ".cursor/rules/ventilation.mdc" in bundle

    def test_each_mdc_has_yaml_frontmatter(self, sample_rules):
        bundle = render_cursor_mdc(sample_rules)
        fire_mdc = bundle[".cursor/rules/fire.mdc"]
        assert fire_mdc.startswith("---\n")
        assert "description:" in fire_mdc
        assert "globs:" in fire_mdc
        assert "alwaysApply: false" in fire_mdc

    def test_globs_scoped_per_category(self, sample_rules):
        bundle = render_cursor_mdc(sample_rules)
        fire_mdc = bundle[".cursor/rules/fire.mdc"]
        assert '"**/fire/**"' in fire_mdc
        # structure mdc should not contain fire globs
        struct_mdc = bundle[".cursor/rules/structure.mdc"]
        assert '"**/fire/**"' not in struct_mdc
        assert '"**/struct*"' in struct_mdc

    def test_includes_rule_text_and_citation(self, sample_rules):
        bundle = render_cursor_mdc(sample_rules)
        fire_mdc = bundle[".cursor/rules/fire.mdc"]
        assert "Fire doors must provide at least 30 minutes of resistance." in fire_mdc
        assert "AD-B" in fire_mdc
        assert "p.42" in fire_mdc

    def test_legacy_cursorrules_flat_file(self, sample_rules):
        bundle = render_cursor_mdc(sample_rules)
        assert ".cursorrules" in bundle
        flat = bundle[".cursorrules"]
        assert "UK Building Regulations" in flat
        assert "Fire" in flat
        assert "Structure" in flat

    def test_empty_rules_emits_only_flat_file(self):
        bundle = render_cursor_mdc([])
        # No categories → only the (empty) legacy file
        assert list(bundle.keys()) == [".cursorrules"]


class TestRenderClaudeMd:
    def test_filename_is_claude_md(self, single_rule):
        name, _ = render_claude_md(single_rule)
        assert name == "CLAUDE.md"

    def test_has_usage_instructions(self, single_rule):
        _, content = render_claude_md(single_rule)
        assert "How to use this file" in content
        assert "rule_id" in content

    def test_rule_count_in_header(self, sample_rules):
        _, content = render_claude_md(sample_rules)
        assert "3 active rules" in content
        assert "3 categories" in content

    def test_each_rule_is_citation_bearing(self, sample_rules):
        _, content = render_claude_md(sample_rules)
        for r in sample_rules:
            assert r.rule_id in content
            assert f"p.{r.citation.page}" in content

    def test_parameters_rendered_inline(self, sample_rules):
        _, content = render_claude_md(sample_rules)
        assert "min_resistance_min=30" in content


class TestRenderConsolidatedMd:
    def test_filename(self, single_rule):
        name, _ = render_consolidated_md(single_rule)
        assert name == "CONSOLIDATED_REGULATIONS.md"

    def test_includes_all_sections(self, sample_rules):
        _, content = render_consolidated_md(sample_rules)
        assert "**Rule ID:**" in content
        assert "**Section:**" in content
        assert "**Category:**" in content
        assert "**Status:**" in content
        assert "**Parameters:**" in content
        assert "**References:**" in content
        assert "**Source:**" in content


class TestRenderJson:
    def test_filename(self, single_rule):
        name, _ = render_json(single_rule)
        assert name == "regulations.json"

    def test_valid_json_payload(self, sample_rules):
        _, content = render_json(sample_rules)
        payload = json.loads(content)
        assert payload["rule_count"] == 3
        assert len(payload["rules"]) == 3
        assert "generated_at" in payload

    def test_rule_round_trips_citation(self, sample_rules):
        _, content = render_json(sample_rules)
        payload = json.loads(content)
        first = payload["rules"][0]
        assert first["citation"]["doc_id"] == "AD-B"
        assert first["citation"]["page"] == 42

    def test_parameters_preserved(self, sample_rules):
        _, content = render_json(sample_rules)
        payload = json.loads(content)
        fire_rule = next(r for r in payload["rules"] if r["rule_id"] == "rule-fire-001")
        assert fire_rule["parameters"] == {"min_resistance_min": 30}


class TestRenderAll:
    def test_bundle_contains_all_artifacts(self, sample_rules):
        bundle = render_all(sample_rules)
        # At least: 3 category .mdc + .cursorrules + CLAUDE.md + consolidated + json
        assert ".cursor/rules/fire.mdc" in bundle
        assert ".cursor/rules/structure.mdc" in bundle
        assert ".cursor/rules/ventilation.mdc" in bundle
        assert ".cursorrules" in bundle
        assert "CLAUDE.md" in bundle
        assert "CONSOLIDATED_REGULATIONS.md" in bundle
        assert "regulations.json" in bundle

    def test_no_empty_files(self, sample_rules):
        bundle = render_all(sample_rules)
        for path, content in bundle.items():
            assert len(content) > 0, f"{path} is empty"


# --------------------------------------------------------------------------- #
#  2. API endpoints
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(monkeypatch) -> TestClient:
    """FastAPI TestClient wired to a mock backend containing sample rules."""
    from uk_regs_api import app as app_module
    from uk_regs_api import service

    rules = [
        _make_rule(
            rule_id="rule-fire-001",
            text="Fire doors must provide at least 30 minutes of resistance.",
            category=Category.FIRE,
            parameters={"min_resistance_min": 30},
        ),
        _make_rule(
            rule_id="rule-struct-001",
            text="Minimum floor live load is 1.5 kN/m².",
            category=Category.STRUCTURE,
            page=12,
            parameters={"min_live_load_kn_m2": 1.5},
        ),
    ]

    class StubBackend:
        def get_current(self, **kwargs):
            return rules

    monkeypatch.setattr(service, "get_backend", lambda: StubBackend())
    monkeypatch.setattr(app_module, "get_backend", lambda: StubBackend())

    return TestClient(app_module.app)


class TestApiExportEndpoints:
    def test_bundle_manifest(self, client):
        resp = client.get("/export/bundle")
        assert resp.status_code == 200
        data = resp.json()
        assert data["rule_count"] == 2
        paths = {f["path"] for f in data["files"]}
        assert ".cursor/rules/fire.mdc" in paths
        assert "CLAUDE.md" in paths

    def test_claude_md_download(self, client):
        resp = client.get("/export/claude.md")
        assert resp.status_code == 200
        body = resp.text
        assert "CLAUDE.md" not in resp.headers.get("content-disposition", "")  # no forced dl
        assert "UK Building Regulations" in body
        assert "rule-fire-001" in body

    def test_consolidated_md_download(self, client):
        resp = client.get("/export/consolidated.md")
        assert resp.status_code == 200
        assert "CONSOLIDATED_REGULATIONS" not in resp.text  # it's the body, not filename
        assert "Fire doors" in resp.text

    def test_json_download(self, client):
        resp = client.get("/export/regulations.json")
        assert resp.status_code == 200
        data = resp.json()
        assert data["rule_count"] == 2
        ids = {r["rule_id"] for r in data["rules"]}
        assert ids == {"rule-fire-001", "rule-struct-001"}

    def test_cursor_mdc_existing_category(self, client):
        resp = client.get("/export/cursor/fire.mdc")
        assert resp.status_code == 200
        assert resp.text.startswith("---")
        assert "Fire doors" in resp.text

    def test_cursor_mdc_missing_category_404(self, client):
        resp = client.get("/export/cursor/nonexistent.mdc")
        assert resp.status_code == 404
        detail = resp.json()["detail"]
        assert "nonexistent" in detail
        assert "fire" in detail  # lists available

    def test_cursorrules_download(self, client):
        resp = client.get("/export/cursorrules")
        assert resp.status_code == 200
        assert "UK Building Regulations" in resp.text

    def test_legacy_markdown_still_works(self, client):
        """The original /export/markdown endpoint must remain backward-compatible."""
        resp = client.get("/export/markdown")
        assert resp.status_code == 200
        data = resp.json()
        assert data["filename"] == "CONSOLIDATED_REGULATIONS.md"
        assert data["rule_count"] == 2
        assert "Fire doors" in data["content"]


# --------------------------------------------------------------------------- #
#  3. CLI — dry-run from a JSON file
# --------------------------------------------------------------------------- #
class TestCli:
    def test_dry_run_from_json(self, sample_rules, tmp_path, monkeypatch):
        """The CLI should dry-run list artifacts from a regulations.json file."""
        # Write a regulations.json the CLI can read
        _, json_content = render_json(sample_rules)
        json_file = tmp_path / "regulations.json"
        json_file.write_text(json_content, encoding="utf-8")

        from typer.testing import CliRunner
        from uk_regs_export.cli import app as cli_app

        runner = CliRunner()
        result = runner.invoke(
            cli_app,
            ["render", "--json", str(json_file), "--out", str(tmp_path), "--dry-run"],
        )

        assert result.exit_code == 0, result.output
        assert "would generate" in result.output.lower() or "Dry run" in result.output
        assert ".cursor/rules/fire.mdc" in result.output
        assert "CLAUDE.md" in result.output

    def test_render_writes_files(self, sample_rules, tmp_path):
        """The CLI should write the full bundle to disk."""
        _, json_content = render_json(sample_rules)
        json_file = tmp_path / "regulations.json"
        json_file.write_text(json_content, encoding="utf-8")

        from typer.testing import CliRunner
        from uk_regs_export.cli import app as cli_app

        out_dir = tmp_path / "export"
        runner = CliRunner()
        result = runner.invoke(
            cli_app,
            ["render", "--json", str(json_file), "--out", str(out_dir)],
        )

        assert result.exit_code == 0, result.output
        assert (out_dir / ".cursor" / "rules" / "fire.mdc").exists()
        assert (out_dir / "CLAUDE.md").exists()
        assert (out_dir / "regulations.json").exists()
        assert (out_dir / "CONSOLIDATED_REGULATIONS.md").exists()

    def test_render_requires_source(self, tmp_path):
        """Without --api or --json, the CLI should error out."""
        from typer.testing import CliRunner
        from uk_regs_export.cli import app as cli_app

        runner = CliRunner()
        result = runner.invoke(cli_app, ["render", "--out", str(tmp_path)])
        assert result.exit_code == 1
        assert "api" in result.output.lower() or "json" in result.output.lower()
