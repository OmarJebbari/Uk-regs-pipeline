# UK Regs Pipeline

> A **versioned, cited, GraphRAG context-intelligence pipeline** for UK Building Regulations.
> Turns regulation PDFs (text + diagrams) into a single, queryable "current truth" that an
> IDE assistant (Cursor / Claude / Copilot via MCP) can use to update code — with every claim
> **cited to a source page**.

---

## Why this exists

UK building regulations are updated periodically (new Approved Documents, amended standards).
Each release is a dense PDF of rules + diagrams. Engineering teams need to:

1. **Remember** every version of every rule (nothing is lost).
2. **Update** the working context when a new PDF lands (what changed? what's superseded?).
3. **Consume** that context inside their tools (IDE assistant) so code/config can be regenerated
   against the latest rules — **with citations**, because compliance is critical.

This pipeline automates all three.

---

## Architecture (10-second version)

```
PDF → Intake → Document Intelligence → Deterministic Facts → LLM Rule Extraction
    → Figure Understanding (VLM) → Embeddings → Reconciliation Engine
    → Knowledge Graph (Neo4j) + Postgres/pgvector → Context Serving API + MCP Server → IDE
```

### System architecture diagram

> 🌙 **Dark-mode native.** A deep-navy canvas with vibrant, glowing nodes and white text —
> designed to look striking on GitHub's dark theme (still perfectly readable in light mode).
> Rendered with a hand-drawn (sketch) outline for a modern whiteboard feel.

```mermaid
---
config:
  look: handDrawn
  handDrawnSeed: 7
  theme: base
  themeVariables:
    background: "#0b1220"
    fontFamily: '"Segoe UI", "Inter", system-ui, sans-serif'
    fontSize: 14px
    primaryColor: "#1e293b"
    primaryTextColor: "#f8fafc"
    primaryBorderColor: "#6366f1"
    lineColor: "#94a3b8"
    secondaryColor: "#0f1a30"
    tertiaryColor: "#13203a"
    mainBkg: "#0f1a30"
    clusterBkg: "#0f1a30"
    clusterBorder: "#334155"
    edgeLabelBackground: "#0b1220"
---
flowchart TD
    %% ════════════════ SOURCES ════════════════
    subgraph SRC["📥 Sources"]
        direction LR
        S1(["gov.uk<br/>legislation.gov.uk"]):::src
        S2(["Manual upload<br/>drop folder"]):::src
    end

    %% ════════════════ INTAKE ════════════════
    subgraph INGEST["⚙️ Intake & Storage"]
        I1[["Hash · dedup · virus scan"]]:::ingest
        I2[("Object Store<br/>MinIO · S3<br/><i>immutable originals</i>")]:::store
        I3["Document manifest<br/>doc_id · version · published_date"]:::ingest
        I1 --> I2 --> I3
    end

    %% ════════════════ DOCUMENT INTELLIGENCE ════════════════
    subgraph DOCINT["🔍 Document Intelligence"]
        D1["Layout-aware parse<br/>Docling · LlamaParse · PyMuPDF"]:::parse
        D2["OCR fallback<br/>PaddleOCR · Tesseract"]:::parse
        D3["Blocks + tables<br/>page_no · bbox"]:::parse
        D4["Figure extraction<br/>image + caption"]:::parse
        D1 --> D2 --> D3 --> D4
    end

    %% ════════════════ EXTRACTION ════════════════
    subgraph EXTRACT["🧠 Extraction"]
        E1["Deterministic extractors<br/>dimensions · BS EN refs · section IDs"]:::ai
        E2["LLM rule extractor<br/>structured outputs + Pydantic"]:::ai
        E3["VLM figure understander<br/>GPT-4o · Claude · Gemini"]:::ai
        E4["Embeddings<br/>text-embedding-3-small + ColPali"]:::ai
        E1 --> E2 --> E3 --> E4
    end

    %% ════════════════ KNOWLEDGE ════════════════
    subgraph KNOW["💾 Knowledge Plane — bitemporal"]
        K1[("PostgreSQL + pgvector<br/>rules · manifests · change-sets · vectors")]:::db
        K2[("Neo4j<br/>supersession · references · dependencies")]:::db
        K1 <-.-> K2
    end

    %% ════════════════ RECONCILIATION ════════════════
    subgraph RECON["🔁 Reconciliation Engine"]
        R1["Match new rules<br/>vs current corpus"]:::recon
        R2{"Decision<br/>pair + classify"}:::decision
        R1 --> R2
        R2 -->|match| R3["unchanged"]:::recon
        R2 -->|partial| R4["amended<br/>SUPERSEDED_BY edge"]:::recon
        R2 -->|novel| R5["added"]:::recon
        R2 -->|conflict| R6{{"human review"}}:::warn
        R7["ChangeSet<br/>proposed → approved"]:::recon
        R3 & R4 & R5 --> R7
        R6 --> R7
    end

    %% ════════════════ QUALITY ════════════════
    subgraph QA["✅ Quality & Eval"]
        direction LR
        Q1["Golden set"]:::qa
        Q2["DeepEval · RAGAS"]:::qa
        Q3["Confidence routing"]:::qa
    end

    %% ════════════════ SERVING ════════════════
    subgraph SERVE["🚀 Context Serving"]
        A1["FastAPI<br/>REST · RAG endpoints"]:::serve
        A2["MCP server<br/>tools for IDE"]:::serve
        A3[".cursor/rules · CLAUDE.md<br/>static export"]:::serve
    end

    %% ════════════════ CONSUMER ════════════════
    subgraph IDE["🧑‍💻 IDE / Assistant"]
        C1["Cursor · Claude · Copilot<br/><b>edits code from cited context</b>"]:::ide
    end

    %% ════════════════ CROSS-CUTTING ════════════════
    OBS(["🔎 Observability<br/>OpenTelemetry + Langfuse"]):::obs
    OBS -.-> EXTRACT
    OBS -.-> RECON
    OBS -.-> SERVE

    %% ════════════════ MAIN FLOW ════════════════
    SRC ==> INGEST ==> DOCINT ==> EXTRACT ==> RECON ==> KNOW ==> SERVE ==> IDE
    QA ==> RECON

    %% ════════════════ DARK-MODE GLOW PALETTE ════════════════
    classDef src      fill:#0c2740,stroke:#38bdf8,stroke-width:2px,color:#e0f2fe
    classDef ingest   fill:#0b253b,stroke:#0ea5e9,stroke-width:2px,color:#f0f9ff
    classDef store    fill:#112c4f,stroke:#3b82f6,stroke-width:2.5px,color:#dbeafe
    classDef parse    fill:#2a160a,stroke:#f97316,stroke-width:2px,color:#ffedd5
    classDef ai       fill:#241043,stroke:#a855f7,stroke-width:2.5px,color:#f3e8ff
    classDef db       fill:#082c1f,stroke:#10b981,stroke-width:2.5px,color:#d1fae5
    classDef recon    fill:#3a0d24,stroke:#ec4899,stroke-width:2px,color:#fce7f3
    classDef decision fill:#3a2606,stroke:#f59e0b,stroke-width:2.5px,color:#fef3c7
    classDef warn     fill:#3b0a0a,stroke:#ef4444,stroke-width:3px,color:#fee2e2
    classDef qa       fill:#332604,stroke:#eab308,stroke-width:2px,color:#fef9c3
    classDef serve    fill:#1f1340,stroke:#8b5cf6,stroke-width:2.5px,color:#ede9fe
    classDef ide      fill:#020617,stroke:#6366f1,stroke-width:3px,color:#f8fafc
    classDef obs      fill:#111827,stroke:#64748b,stroke-width:2px,stroke-dasharray:5 5,color:#e2e8f0

    %% ════════════════ LINK STYLING ════════════════
    linkStyle default stroke:#64748b,stroke-width:2px
    linkStyle 0,1,2,3,4,5,6,7 stroke:#818cf8,stroke-width:3px
```

| Principle | How |
|---|---|
| **Provenance** | Every rule stores `doc_id`, `version`, `page`, `bbox`, `raw_text`. No citation = no rule. |
| **Deterministic-first** | Regex/grammar extract dimensions, standards refs, section IDs *before* any LLM call. |
| **Bitemporal memory** | `valid_from`/`valid_to` + supersession edges. Old rules are never deleted, only superseded. |
| **Human-in-the-loop** | Change-sets are `proposed` until reviewed/approved; nothing goes live automatically by default. |
| **Graceful degradation** | Runs offline (deterministic-only) if no LLM/embedder is configured. |

---

## Monorepo layout

```
uk-regs-pipeline/
├─ packages/
│  ├─ core/           # types, config, enums, errors, ID helpers (pydantic-settings)
│  ├─ extraction/     # PDF parsing, deterministic extractors, LLM rule extractor, VLM figures
│  ├─ knowledge/      # object store, relational + graph repos, embeddings, schema.sql
│  └─ reconciliation/ # matchers + supersession engine (the "update memory" heart)
├─ pipelines/
│  └─ ingest/         # orchestrator + CLI (`uk-regs-ingest`)
├─ apps/              # (planned) FastAPI + MCP server
├─ infra/
│  ├─ compose/        # docker-compose: Postgres+pgvector, Neo4j, MinIO
│  ├─ docker/         # (Dockerfiles)
│  └─ terraform/      # (infra-as-code)
├─ tests/
├─ data/              # sample PDFs (gitignored)
├─ pyproject.toml     # uv workspace root
├─ Makefile
└─ .env.example
```

---

## Quick start

### Prerequisites
- **Python 3.12+**
- **[uv](https://docs.astral.sh/uv/)** (fast Python package manager)
- **Docker** (for Postgres, Neo4j, MinIO)
- *(optional)* **GNU Make** — or run the commands inside the `Makefile` manually.

### 1. Install
```bash
make install          # uv sync — installs all workspace packages in editable mode
cp .env.example .env  # then edit .env (set OPENAI_API_KEY to use the LLM)
```

### 2. Start the data plane
```bash
make up        # Postgres+pgvector (5432), Neo4j (7474/7687), MinIO (9000/9001)
make init-db   # apply schema.sql (idempotent)
```

### 3. Ingest a PDF
```bash
# Full pipeline (needs OPENAI_API_KEY):
make ingest PDF=path/to/adb.pdf TITLE="Approved Document B" VER="2026-ed1"

# Offline / deterministic-only (no API key, no network):
make ingest-offline PDF=path/to/adb.pdf TITLE="Approved Document B" VER="2026-ed1"
```

The CLI prints a rich summary: pages parsed, blocks, figures, rules extracted,
reconciliation decisions (`added`/`amended`/`superseded`/`unchanged`),
how many need review, and the `change_set_id`.

### 4. Direct CLI usage
```bash
uv run uk-regs-ingest --help
uv run uk-regs-ingest ingest data/sample.pdf --title "Approved Document B" --version 2026-ed1 --skip-llm
uv run uk-regs-ingest batch data/pdfs/ --version 2026-ed1 --skip-llm
```

---

## How "memory + update" works

Each extracted rule is a **typed Pydantic object** with provenance and bitemporal fields.
The **reconciliation engine** compares a new rule against the current corpus:

1. Embed the new rule; retrieve top-k candidate old rules in the same domain.
2. An LLM **pairs** them into `match | partial | novel | conflict` (with rationale + confidence).
3. Outcomes:
   - `match` → unchanged
   - `partial` → **amended**: create a `SUPERSEDED_BY` edge, close the old `valid_to`
   - `novel` → added
   - `conflict` → flagged for human review
4. The result is persisted as a **ChangeSet** (`proposed` status by default).
5. After review/approval (`--apply`), the new rules become `active` and form the new "current truth".

Old versions are **never deleted** — they remain queryable for audit history.

---

## Configuration

All config is 12-factor via `.env` (see `.env.example`), parsed by
`uk_regs_core.config.Settings` (pydantic-settings). Key knobs:

| Var | Default | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | — | Enables LLM extraction + figure understanding |
| `LLM_PROVIDER` | `openai` | `openai` / `anthropic` / `bedrock` / `vllm` |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | 1536-dim OpenAI embeddings |
| `CONFIDENCE_THRESHOLD` | `0.85` | Below this → route to human review |
| `RECONCILIATION_TOP_K` | `5` | Candidates per new rule during matching |

---

## Development

```bash
make fmt      # ruff format + autofix
make lint     # ruff check
make test     # pytest
make check    # lint + test
make logs     # tail data-plane logs
make reset    # ⚠ stop services and DELETE all volumes
```

---

## Roadmap

- [x] **Phase 1** — Vertical slice: ingest → parse → extract → persist → CLI
- [x] **Phase 2** — Bitemporal memory + reconciliation engine
- [ ] **Phase 3** — FastAPI context-serving API + **MCP server** (Cursor/Claude integration)
- [ ] **Phase 4** — Evals (golden set, DeepEval/RAGAS), review UI, auth, observability
- [ ] **Phase 5** — Scheduling/source monitoring, hybrid search, dashboards

---

## License & data

Source code: see `LICENSE`. UK regulation PDFs remain Crown copyright / OGL where applicable;
**do not commit** copyrighted PDFs to this repo — place them under `data/` (gitignored).