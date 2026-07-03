# =============================================================================
# UK Regs Pipeline — developer task runner
# Requires: Python 3.12+, `uv` (https://docs.astral.sh/uv), Docker, GNU Make.
# =============================================================================

COMPOSE := docker compose -f infra/compose/docker-compose.yml
SCHEMA  := packages/knowledge/src/uk_regs_knowledge/relational/schema.sql
PDF     ?= data/sample.pdf
TITLE   ?= "Approved Document B"
VER     ?= "2026-ed1"

.PHONY: help install up down status logs init-db reset \
        ingest ingest-offline lint fmt test check clean

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# --------------------------------------------------------------------------- #
#  Environment
# --------------------------------------------------------------------------- #
install:  ## Install/ sync all workspace packages via uv
	uv sync

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

check: lint test  ## lint + test

# --------------------------------------------------------------------------- #
#  Misc
# --------------------------------------------------------------------------- #
clean:  ## Remove build/test artifacts
	rm -rf .pytest_cache .ruff_cache **/__pycache__ dist build *.egg-info