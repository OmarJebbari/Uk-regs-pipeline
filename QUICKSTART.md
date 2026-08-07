# 🚀 Quickstart — How to Test & Use the Pipeline

> Read this if you want to **run the thing**, not read about it.

---

## 0. Prerequisites

| Tool | Why | Install |
|------|-----|---------|
| **Python 3.11+** | Runs the pipeline | [python.org](https://python.org) |
| **uv** | Fast Python package manager | `pip install uv` or [astral.sh](https://docs.astral.sh/uv/) |
| **Docker Desktop** | Runs Postgres + pgvector + Neo4j + MinIO | [docker.com](https://docker.com) |
| **GNU Make** *(optional)* | Shortcut commands | Windows: `choco install make` or use `winget install GnuWin32.Make` |
| **OpenAI API key** | Only needed for LLM extraction / figure understanding | [platform.openai.com](https://platform.openai.com) |

You have `uv 0.10.8` and Python 3.14.3 — good to go. ✅

---

## 1. First-Time Setup (do this once)

```cmd
:: 1. Open PowerShell/cmd in the project folder
cd C:\Users\jebba\Desktop\uk-regs-pipeline

:: 2. Install all Python packages (the workspace + CLI entry points)
uv sync

:: 3. Create your .env from the template
copy .env.example .env

:: 4. (Optional) Add your OpenAI key to .env — only if you want LLM extraction
::    Open .env in a text editor and set:
::    OPENAI_API_KEY=sk-...your-key...
```

That's it. You're set up.

---

## 2. The 30-Second Smoke Test (no Docker, no API key)

This proves the pipeline **parses PDFs and extracts rules** — using deterministic
extractors only, no database required.

```cmd
:: Windows (cmd)
uv run uk-regs-ingest ingest data\sample.pdf --title "Approved Document B" --version "2026-ed1" --skip-llm --skip-embed
```

You'll see output like:

```
Ingesting sample.pdf
INFO    ingest start: APPROVED_DOCUMENT_B v=2026-ed1
INFO    extracted 5 rules from APPROVED_DOCUMENT_B
┌─────────────────────────────────────────┐
│ pages           │ 2                     │
│ blocks          │ 12                    │
│ rules extracted │ 5                     │
│ applied         │ no (proposed)         │
└─────────────────────────────────────────┘
```

✅ **This confirms extraction works.** The warnings about Postgres are expected —
nothing is persisted until you start the database (next step).

---

## 3. Full Local Stack (Docker — Postgres + pgvector + Neo4j + MinIO)

This gives you **persistence, hybrid search, and the review UI**.

### 3a. Start the data services

```cmd
:: Start Postgres (with pgvector), Neo4j, and MinIO in the background
docker compose -f infra\compose\docker-compose.yml up -d

:: Check they're healthy
docker compose -f infra\compose\docker-compose.yml ps
```

### 3b. Initialize the database schema

```cmd
uv run uk-regs-ingest init-db --schema packages\knowledge\src\uk_regs_knowledge\relational\schema.sql
```

### 3c. Run a real ingest (now with persistence!)

**Without LLM** (deterministic only — no API key needed):

```cmd
uv run uk-regs-ingest ingest data\sample.pdf --title "Approved Document B" --version "2026-ed1" --skip-llm --skip-embed
```

**With LLM** (needs `OPENAI_API_KEY` in `.env`) — higher accuracy + figure understanding:

```cmd
uv run uk-regs-ingest ingest data\sample.pdf --title "Approved Document B" --version "2026-ed1"
```

Rules are now stored in Postgres and embeddings in pgvector.

---

## 4. The Context API + Web UI

The **FastAPI server** exposes the extracted context to your IDE and lets you
review proposed rule changes.

### 4a. Start the API

```cmd
:: Auto mode: uses Postgres if running, otherwise falls back to mock backend
uv run uk-regs-api
```

Open these in your browser:

| URL | What |
|-----|------|
| `http://localhost:8000/docs` | **Swagger API docs** — try every endpoint live |
| `http://localhost:8000/ui/` | **ChangeSet Review UI** — approve/reject rule changes |
| `http://localhost:8000/ops` | **Ops Dashboard** — live pipeline stats (HTMX, auto-refresh) |

### 4b. Search for context (hybrid: BM25 + vector)

```cmd
:: Semantic + keyword search across all ingested rules
curl "http://localhost:8000/search?q=fire+escape+routes&top_k=5"
```

### 4c. Export the consolidated context

```cmd
:: Human-readable consolidated rules → CONSOLIDATED_REGULATIONS.md
curl http://localhost:8000/export/markdown > CONSOLIDATED_REGULATIONS.md

:: Machine-readable JSON
curl http://localhost:8000/export/regulations.json > regulations.json

:: Claude Code / generic LLM format
curl http://localhost:8000/export/claude.md > CLAUDE.md
```

---

## 5. IDE Integration (Cursor / Claude Code)

This is the payoff: **the pipeline feeds context directly into your AI coding
assistant.**

### Option A — MCP server (live, always-current)

The MCP (Model Context Protocol) server lets Cursor/Claude **call the pipeline
in real-time** while you code.

**Cursor:** `Settings → MCP → Add Server`:

```json
{
  "command": "uv",
  "args": ["run", "--directory", "C:\\Users\\jebba\\Desktop\\uk-regs-pipeline", "uk-regs-mcp"],
  "env": {
    "UK_REGS_BACKEND": "auto"
  }
}
```

You'll get tools like `uk_regs__search_context` and `uk_regs__get_rule` available
to the AI while coding.

### Option B — Static export (commit the rules into your repo)

```cmd
:: Render the full IDE bundle: .cursor/rules/*.mdc, CLAUDE.md, JSON
uv run uk-regs-export render --api http://localhost:8000 --out dist\export
```

Then copy the `.mdc` files into `.cursor/rules/` (Cursor) or the `CLAUDE.md`
into your project root (Claude Code). The AI now reads the consolidated building
regs context on every conversation.

---

## 6. Automated Source Monitoring

The pipeline can **watch publishers for new PDFs and auto-ingest them** — so the
context stays current as the UK publishes new editions.

### 6a. Configure sources

Edit `sources.yaml` (already has example UK Building Regs feeds):

```yaml
sources:
  - id: gov-adb
    name: "Approved Document B"
    url: "https://assets.publishing.service.gov.uk/.../approved-document-b.pdf"
    poll_interval_minutes: 1440   # daily
```

### 6b. Run a poll (manual or via cron/GitHub Actions)

```cmd
:: One-shot: check all sources, ingest any new/changed PDFs
uv run uk-regs-sourcing --once --apply

:: Or long-running scheduler (polls every 60 min)
uv run uk-regs-sourcing --interval 60 --apply
```

### 6c. Or trigger a poll via the API

```cmd
curl -X POST "http://localhost:8000/sources/poll?force=true"
```

---

## 7. Make Shortcuts (if you have Make installed)

Every command above has a `make` shortcut. Run `make help` to see them all:

```cmd
make help
make up            :: start data services
make init-db       :: create schema
make ingest        :: ingest data/sample.pdf (with LLM)
make ingest-offline:: ingest without LLM/API key
make api           :: start the API
make eval          :: run the eval suite (quality gate)
make test          :: run the test suite
```

---

## 8. Quality Gate — Evals & Tests

The pipeline ships with a **golden-set eval harness** — regression tests that
prove extraction/reconciliation accuracy doesn't degrade.

```cmd
:: Run the full eval suite (uses Postgres if up, else mock backend)
uv run uk-regs-eval run

:: List the golden test cases without running
uv run uk-regs-eval list

:: Run the unit + integration tests
uv run pytest -q
```

---

## 9. Production Deployment (AWS via Terraform)

Everything is codified. From `infra/terraform/`:

```cmd
cd infra\terraform

:: Initialize Terraform
terraform init

:: See what will be created (RDS+pgvector, S3, ECS Fargate, etc.)
terraform plan -var-file=dev.tfvars

:: Provision everything (pass secrets via env vars, NOT the command line)
set TF_VAR_openai_api_key=sk-...
set TF_VAR_api_key=some-random-secret
terraform apply -var-file=dev.tfvars -auto-approve

:: Get the live endpoint
terraform output
```

Or go full-Docker:

```cmd
:: Build + run the entire app stack (API + worker + MCP) in containers
docker compose -f infra\compose\docker-compose.yml ^
               -f infra\compose\docker-compose.app.yml ^
               --profile ingest up -d --build api
```

---

## 10. Troubleshooting

| Symptom | Fix |
|---------|-----|
| `Failed to spawn: uk-regs-ingest` | Run `uv sync` first to install CLI entry points |
| `psycopg/pool not available` | The DB isn't running. Either `docker compose up -d` or use `--skip-embed` with the mock backend |
| `OPENAI_API_KEY not set` | Add it to `.env`, or use `--skip-llm` for deterministic-only extraction |
| `port 8000 already in use` | Set `PORT=8001` in `.env` and restart the API |
| `make: command not found` | Install Make, or use the raw `uv run ...` commands directly |

---

## TL;DR — Minimum Viable Demo

```cmd
cd uk-regs-pipeline
uv sync
copy .env.example .env
uv run uk-regs-ingest ingest data\sample.pdf --title "ADB" --version "2026" --skip-llm --skip-embed
```

That's the pipeline parsing a PDF and extracting rules in three commands. 🎉