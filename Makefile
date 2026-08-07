# =============================================================================
# UK Regs Pipeline — developer task runner
# Requires: Python 3.12+, `uv` (https://docs.astral.sh/uv), Docker, GNU Make.
# =============================================================================

COMPOSE     := docker compose -f infra/compose/docker-compose.yml
COMPOSE_APP := docker compose \
                 -f infra/compose/docker-compose.yml \
                 -f infra/compose/docker-compose.app.yml
SCHEMA  := packages/knowledge/src/uk_regs_knowledge/relational/schema.sql
PDF     ?= data/sample.pdf
TITLE   ?= "Approved Document B"
VER     ?= "2026-ed1"

.PHONY: help install hooks up down status logs init-db reset \
        ingest ingest-offline lint fmt test check ci clean \
        api api-mock api-protected api-otel mcp \
        export-rules export-cursor export-claude export-bundle export-json \
        review ops \
        eval eval-offline eval-list \
        source-once source-watch source-list source-poll \
        compose-up-app compose-down-app compose-build-app compose-logs-app \
        docker-ingest docker-shell image-build image-push

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

api-protected:  ## Run the API with API_KEY gating (set API_KEY in .env first)
	@echo "Starting protected API — mutating routes need X-API-Key"
	UK_REGS_BACKEND=auto uv run uk-regs-api

api-otel:  ## Run the API with OpenTelemetry traces → local OTLP collector
	@echo "Ensure an OTLP collector is listening on \$$OTEL_EXPORTER_OTLP_ENDPOINT"
	UK_REGS_BACKEND=auto uv run uk-regs-api

mcp:  ## Run the MCP server over stdio (for Cursor/Claude IDE integration)
	uv run uk-regs-mcp

export-rules:  ## Export consolidated regulations to CONSOLIDATED_REGULATIONS.md
	@uv run python -c "import json, urllib.request; \
		r = urllib.request.urlopen('http://localhost:8000/export/markdown'); \
		d = json.loads(r.read()); \
		open(d['filename'], 'w', encoding='utf-8').write(d['content']); \
		print(f'Wrote {d[\"filename\"]} ({d[\"rule_count\"]} rules)')"

export-cursor:  ## Render the full IDE bundle (.cursor/rules, CLAUDE.md, JSON) to dist/export/
	@echo "Rendering IDE context bundle to dist/export/ ..."
	@uv run uk-regs-export render --api http://localhost:8000 --out dist/export

export-claude:  ## Download CLAUDE.md to the project root
	@curl -s http://localhost:8000/export/claude.md -o CLAUDE.md
	@echo "Wrote CLAUDE.md"

export-bundle:  ## List every artifact the export can generate (JSON manifest)
	@curl -s http://localhost:8000/export/bundle | python -m json.tool

export-json:  ## Download regulations.json (machine-readable)
	@curl -s http://localhost:8000/export/regulations.json -o regulations.json
	@echo "Wrote regulations.json"

review:  ## Open the ChangeSet Review UI (HTMX dashboard) in the default browser
	@echo "Opening Review UI at http://localhost:8000/ui/ ..."
	@command -v xdg-open >/dev/null 2>&1 && xdg-open http://localhost:8000/ui/ || \
	 command -v open >/dev/null 2>&1 && open http://localhost:8000/ui/ || \
	 start http://localhost:8000/ui/ || true

ops:  ## Open the Ops Dashboard (live pipeline stats) in the default browser
	@echo "Opening Ops Dashboard at http://localhost:8000/ops ..."
	@command -v xdg-open >/dev/null 2>&1 && xdg-open http://localhost:8000/ops || \
	 command -v open >/dev/null 2>&1 && open http://localhost:8000/ops || \
	 start http://localhost:8000/ops || true

# --------------------------------------------------------------------------- #
#  Source monitoring — auto-ingest new/changed PDFs (Phase 5-1)
# --------------------------------------------------------------------------- #
source-list:  ## List configured sources
	uv run uk-regs-sourcing --once --skip-llm || true

source-once:  ## Run one poll cycle (cron / GHA) — ingests new/changed PDFs
	uv run uk-regs-sourcing --once --apply

source-watch:  ## Long-running scheduler (polls every $(INTERVAL) min)
	uv run uk-regs-sourcing --interval 60 --apply

source-poll:  ## Trigger a poll via the API (needs `make api` running)
	@curl -s -X POST "http://localhost:8000/sources/poll?force=true" | python -m json.tool

# --------------------------------------------------------------------------- #
#  Containerised application (API + worker + MCP) — Phase 7
# --------------------------------------------------------------------------- #
compose-up-app:  ## Build + start the full stack: data plane + API (+ profiles: ingest, mcp)
	$(COMPOSE_APP) --profile ingest up -d --build api
	@echo "API on http://localhost:8000  (docs: /docs, UI: /ui, ops: /ops)"

compose-down-app:  ## Stop the app layer (keeps data volumes intact)
	$(COMPOSE_APP) down

compose-build-app:  ## Build all app images without starting them
	$(COMPOSE_APP) build api worker mcp

compose-logs-app:  ## Tail app-layer logs (api + worker)
	$(COMPOSE_APP) logs -f --tail=200 api worker

docker-ingest:  ## Ingest a PDF through the containerised worker
	@test -n "$(PDF)" || (echo "Usage: make docker-ingest PDF=path/to/doc.pdf TITLE='ADB' VER='2026'" && exit 1)
	$(COMPOSE_APP) run --rm worker ingest /pdfs/$(notdir $(PDF)) \
		--title "$(TITLE)" --version "$(VER)" --apply

docker-shell:  ## Open a shell in the API container (debug)
	$(COMPOSE_APP) run --rm --entrypoint /bin/sh api

# Build a single app image ad-hoc. Usage: make image-build SVC=api
image-build:
	@test -n "$(SVC)" || (echo "Usage: make image-build SVC=api|worker|mcp" && exit 1)
	docker build -f infra/docker/Dockerfile.$(SVC) -t uk-regs-$(SVC):latest .

# Build + push to a registry. Usage: make image-push SVC=api REG=ghcr.io/org
image-push:
	@test -n "$(SVC)" || (echo "Usage: make image-push SVC=api REG=ghcr.io/org" && exit 1)
	@test -n "$(REG)"  || (echo "Set REG, e.g. REG=ghcr.io/your-org" && exit 1)
	docker tag uk-regs-$(SVC):latest $(REG)/uk-regs-$(SVC):latest
	docker push $(REG)/uk-regs-$(SVC):latest

# --------------------------------------------------------------------------- #
#  Terraform — AWS infrastructure (Phase 14)
# --------------------------------------------------------------------------- #
# Set TF_ENV=prod for production. Secrets must come from env vars, e.g.:
#   TF_VAR_openai_api_key=sk-... make tf-apply
#   TF_VAR_api_key=$(openssl rand -hex 32) make tf-apply TF_ENV=prod

tf-init:  ## Initialise the backend + providers (run once)
	cd $(TF_DIR) && terraform init

tf-fmt:  ## Auto-format + validate Terraform files
	cd $(TF_DIR) && terraform fmt -recursive && terraform validate

tf-plan:  ## Show a plan for the $(TF_ENV) environment
	cd $(TF_DIR) && terraform plan -var-file=$(TF_ENV).tfvars

tf-apply:  ## Apply Terraform for the $(TF_ENV) environment
	cd $(TF_DIR) && terraform apply -var-file=$(TF_ENV).tfvars -auto-approve

tf-destroy:  ## Tear down the $(TF_ENV) environment (destructive)
	cd $(TF_DIR) && terraform destroy -var-file=$(TF_ENV).tfvars

tf-output:  ## Print all stack outputs (endpoint, secret ARNs, URL)
	cd $(TF_DIR) && terraform output

# --------------------------------------------------------------------------- #
#  Misc
# --------------------------------------------------------------------------- #
clean:  ## Remove build/test artifacts
	rm -rf .pytest_cache .ruff_cache **/__pycache__ dist build *.egg-info
