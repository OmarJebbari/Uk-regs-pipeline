# Logic, Accuracy & Improvement Plan — UK Regs Pipeline

**Date:** 2026-08-16 · Companion to `SECURITY-AUDIT.md`

## 1. What the system does (verified end-to-end trace)

```
sources.yaml → Monitor (poll intervals, sha256/version/size diffing)
  → Fetchers (http/local/mock) → new/changed PDFs
  → IngestPipeline:
      parse PDF (pdfplumber + OCR fallback) → blocks + figures
      → LLM rule extraction per block (+ deterministic fallback)
      → figure understanding (LLM)
      → persist: Postgres (docs/versions/rules/figures) + S3 (raw artifacts)
      → embed rules (OpenAI/Cohere/Mock) → pgvector
      → Reconciler: match new rules vs corpus (section-key + embedding + text sim)
        → deterministic decision or LLM verdict (confidence-gated)
      → ChangeSet (PROPOSED deltas) → human review (CONFIDENCE_THRESHOLD=0.85)
  → API: hybrid search (BM25 ∥ vector → RRF fusion, k=60), diff, review, audit
  → MCP server → IDE integration; Export (renderers); Evals (golden + LLM judge)
```

The architecture is genuinely well-designed: layered uv workspace, Protocol-based backend abstraction, review-before-apply governance, and an **evals harness wired into CI** (`uk-regs-eval run --no-retrieval` gate) — rare and excellent for an LLM pipeline.

## 2. Logic & accuracy findings

### A. Correctness / integrity

1. **Continue-on-failure hides data loss** (see audit H1). The pipeline's `_persist_*` methods log-and-continue on *every* failure class. There is no distinction between "optional enrichment failed" (fine) and "primary record not stored" (fatal). This is the single most important accuracy risk: silent gaps in the rule corpus or in change-sets between document versions.

2. **Reconciliation skip = lost diffs.** If `get_current_rules()` fails mid-run (L484), the version is ingested with no diff against the previous corpus. When a new ADB version ships, users would see "no changes" even though rules changed. Needs an explicit `reconcile_skipped` flag persisted on the version row + a repair job that re-reconciles.

3. **Embedding optional-failure degrades search unequally.** Rules persisted without vectors silently drop out of the semantic leg of hybrid search. Suggest a `has_embedding` column check in the ops stats dashboard + a backfill command (`uk-regs-ingest reembed`).

4. **No retry/backoff anywhere in the ingest path** (`retry` count = 0 across pipeline/fetchers despite `tenacity` being a locked dep). Transient OpenAI/Postgres/network hiccups immediately trigger the swallow-and-continue path from finding A1. Adding `tenacity` retries (3×, exponential) around LLM calls and DB writes would eliminate most silent-partial-failure cases at the source.

5. **Idempotency is partial.** Upserts are keyed by natural IDs (good), but there's no run-level transaction boundary: document row can be persisted while rules fail, leaving a "half-ingested" version. Recommend a per-run `ingest_runs` table (status, counts, error digest) so partial runs are visible and re-runnable.

### B. Retrieval accuracy

6. **BM25/vector fusion weights are static.** RRF k=60 is a sensible default, but there's no evaluation of mode (`vector|bm25|hybrid`) differences on real data. The evals harness already computes metrics — add a mode-sweep eval to pick k empirically per corpus.

7. **`search_bm25` / `search_fulltext` LIMIT+rank only.** Verify tsquery construction escapes user input properly for Postgres full-text (`plainto_tsquery` vs `to_tsquery` — the latter breaks on operators). Tests exist (`test_hybrid_search.py`) — worth asserting adversarial query strings (`!&|()*:`).

8. **MockBackend scoring is naive substring overlap** — fine for tests, but ensure evals never accidentally run against it in prod assertions (ties into audit H2).

### C. LLM extraction quality

9. **Temperature 0.0 + single-shot extraction per block** — good baseline. Improvements: log prompt/response hashes for reproducibility (Langfuse is already wired), add per-block confidence calibration checks, and maintain a golden-set regression for extraction prompts (the evals package supports this — extend coverage to extraction, not just retrieval/judging).

10. **`CONFIDENCE_THRESHOLD=0.85` routes to human review** — sensible, but threshold is global and static. Track precision/recall of the auto-accept path per category in evals; tune per doc_type if the review queue gets noisy.

### D. Observability of accuracy

11. **Ops stats exist** (`stats.py`, `get_ops_stats`) but don't surface: rules-without-embeddings count, unreconciled versions, failed ingest runs. These three metrics directly measure the accuracy risks above.

## 3. Improvement roadmap

### Quick wins (≤1 day each)

| # | Change | Where |
|---|--------|-------|
| 1 | Add `permissions: contents: read` to CI + SHA-pin actions | `.github/workflows/ci.yml` |
| 2 | Add `pip-audit`/`uv audit` step to CI | same |
| 3 | Bind dev compose ports to `127.0.0.1` | `infra/compose/docker-compose.yml` |
| 4 | Pin MinIO/compose images (drop `latest`) | same |
| 5 | Fail ingest run when `rules_extracted>0 and rules_persisted==0` | `pipeline.py` |
| 6 | Persist `reconcile_skipped` flag on version row | `pipeline.py` / repo |
| 7 | Wire custom `get_remote_addr` (XFF-aware) into the Limiter, trusted-proxy count configurable | `ratelimit.py` |
| 8 | Disable `/docs`+`/openapi` when `ENV=prod` | `app.py` |
| 9 | URL allow-list + private-CIDR block in `HttpFetcher._http_get` | `fetchers.py` |
| 10 | Add `has_embedding` backfill command + ops metric | ingest CLI + `stats.py` |

### Medium (days)

- **Ingest run ledger** (`ingest_runs` table + status gating tests extension) → makes partial failures visible and re-runnable.
- **Retry policy**: wrap LLM/embedder/DB calls with `tenacity` (already a dependency); configure per-call-class backoff.
- **AUTH_MODE setting** (off/read/write/all) replacing implicit read-bypass; hashed multi-key support (M3).
- **Separate migration job** for ECS deploys (fixes H3 race): one-shot task definition run before service update.
- **Redis-backed rate-limit storage** for multi-replica correctness.
- **Reconciliation repair job**: re-run reconcile for versions flagged `reconcile_skipped`.
- **Adversarial search-input tests** (tsquery operators, unicode, 10k-char queries).

### Longer term (weeks)

- **Event-driven ingest** (S3 → EventBridge → queue → worker) replacing one-shot CLI runs; gives natural retries, DLQ, and per-document isolation.
- **Prompt/extraction evals in CI golden gate** (extend `uk-regs_evals` to extraction quality, not just retrieval).
- **Eval-driven RRF tuning** per document type; consider learned fusion weights.
- **Multi-tenant API keys with scopes + audit binding** (which reviewer approved what, from which client).
- **SBOM + container scanning** (syft/grype or Trivy) in CI; digest-pinned base images via Renovate.
- **Data quality SLAs**: dashboard panels for coverage (rules w/ embeddings), freshness (last successful monitor poll per source), completeness (unreconciled versions) — the three silent-degradation modes this audit identified.

## 4. Positive callouts (keep doing)

- Eval harness + LLM judge + golden gate in CI
- Review-before-apply governance with audit events
- Protocol-typed backend abstraction (Real/Mock) enabling zero-infra CI
- Locked dependencies + Dependabot
- Clean git history and secrets hygiene
- Well-structured uv workspace with clear package boundaries

## 5. Suggested priority order

1. H1 pipeline failure semantics (+ quick wins 5–6)
2. H2 backend fallback guard
3. H3 migration race
4. M1 rate-limit correctness
5. M2/M3 auth hardening
6. M4 SSRF allow-list
7. Accuracy observability (metrics + repair jobs)
8. Everything else