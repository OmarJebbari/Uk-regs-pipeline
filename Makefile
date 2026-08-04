# =============================================================================
# UK Regs Pipeline — developer task runner
# Requires: Python 3.12+, `uv` (https://docs.astral.sh/uv), Docker, GNU Make.
# =============================================================================

COMPOSE := docker compose -f infra/compose/docker-compose.yml
SCHEMA  := packages/knowledge/src/uk_regs_knowledge/relational/schema.sql
PDF     ?= data/sample.pdf
TITLE   ?= "Approved Document B"
VER     ?= "2026-ed1"

.PHONY: help install hooks up down status logs init-db reset \
        ingest ingest-offline lint fmt test check ci clean \
        api api-mock mcp export-rules review eval eval-offline eval-list

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# --------------------------------------------------------------------------- #
#  Environment
# --------------------------------------------------------------------------- #
install:  ## Install/ sync all workspace packages via uv
	uv sync

hooks:  ## Install pre-commit hooks (run once after clone)
	uv sync
	uv run pre-commit install --install-hooks -t pre-commit -t pre-push
	@echo "pre-commit hooks installed."

.env: .env.example
	@if [ ! -f .env ]; then cp .env.example .env && echo "Created .env from .env.example"; fi

# --------------------------------------------------------------------------- #
#  Data plane (Docker Compose)
# --------------------------------------------------------------------------- #
up: .env  ## Start Postgres, Neo4j, MinIO
	$(COMPOSE) up -d
	@echo "Waiting for services..."
	@$(COMPOSE) ps

down:  ## Stop services
	$(COMPOSE) down

status:  ## Show service status
	$(COMPOSE) ps

logs:  ## Tail service logs
	$(COMPOSE) logs -f --tail=100

reset:  ## Stop services AND delete all data volumes (destructive)
	$(COMPOSE) down -v

# --------------------------------------------------------------------------- #
#  Database
# --------------------------------------------------------------------------- #
init-db: up  ## Apply Postgres + pgvector schema (idempotent)
	uv run uk-regs-ingest init-db --schema $(SCHEMA)

# --------------------------------------------------------------------------- #
#  Pipeline
# --------------------------------------------------------------------------- #
ingest:  ## Full ingest of $(PDF) with LLM (needs OPENAI_API_KEY + services)
	uv run uk-regs-ingest ingest $(PDF) --title $(TITLE) --version $(VER)

ingest-offline:  ## Deterministic-only ingest (no LLM, no API key needed)
	uv run uk-regs-ingest ingest $(PDF) --title $(TITLE) --version $(VER) --skip-llm --skip-embed

# --------------------------------------------------------------------------- #
#  Quality
# --------------------------------------------------------------------------- #
lint:  ## Lint with ruff
	uv run ruff check .

fmt:  ## Format with ruff
	uv run ruff format .
	uv run ruff check --fix .

test:  ## Run the test suite
	uv run pytest -q

check: lint test  ## lint + test (local pre-commit equivalent)

ci:  ## The exact quality gate that GitHub Actions runs (lint + test + evals)
	UK_REGS_BACKEND=mock uv run ruff format --check .
	UK_REGS_BACKEND=mock uv run ruff check .
	UK_REGS_BACKEND=mock uv run pytest -q
	UK_REGS_BACKEND=mock uv run uk-regs-eval run --no-retrieval

# --------------------------------------------------------------------------- #
#  Evaluation — golden-set regression gate (Phase 4A)
# --------------------------------------------------------------------------- #
eval:  ## Run all eval suites against the live backend (Postgres if up, else mock)
	UK_REGS_BACKEND=auto uv run uk-regs-eval run

eval-offline:  ## Run evals without the retrieval suite (no DB required)
	UK_REGS_BACKEND=mock uv run uk-regs-eval run --no-retrieval --out reports/eval.md

eval-list:  ## List the golden cases without running them
	uv run uk-regs-eval list

# --------------------------------------------------------------------------- #
#  Context API + MCP server (Phase 3)
# --------------------------------------------------------------------------- #
api:  ## Run the FastAPI context API (auto: Postgres if up, else mock)
	UK_REGS_BACKEND=auto uv run uk-regs-api

api-mock:  ## Run the API forced to the in-memory mock backend (no DB needed)
	UK_REGS_BACKEND=mock uv run uk-regs-api

mcp:  ## Run the MCP server over stdio (for Cursor/Claude IDE integration)
	uv run uk-regs-mcp

export-rules:  ## Export consolidated regulations to CONSOLIDATED_REGULATIONS.md
	@uv run python -c "import json, urllib.request; \
		r = urllib.request.urlopen('http://localhost:8000/export/markdown'); \
		d = json.loads(r.read()); \
		open(d['filename'], 'w', encoding='utf-8').write(d['content']); \
		print(f'Wrote {d[\"filename\"]} ({d[\"rule_count\"]} rules)')"

review:  ## Open the ChangeSet Review UI (HTMX dashboard) in the default browser
	@echo "Opening Review UI at http://localhost:8000/ui/ ..."
	@command -v xdg-open >/dev/null 2>&1 && xdg-open http://localhost:8000/ui/ || \
	 command -v open >/dev/null 2>&1 && open http://localhost:8000/ui/ || \
	 start http://localhost:8000/ui/ || true

# --------------------------------------------------------------------------- #
#  Misc
# --------------------------------------------------------------------------- #
clean:  ## Remove build/test artifacts
	rm -rf .pytest_cache .ruff_cache **/__pycache__ dist build *.egg-info
