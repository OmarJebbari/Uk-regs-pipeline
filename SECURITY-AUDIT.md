# Security Audit — UK Regs Pipeline

**Date:** 2026-08-16 · **Scope:** full repo @ `main` (code, CI, Docker, Terraform, git history, dependencies)

## Executive Summary

Overall security posture is **good for a project of this maturity**. Notably strong areas:

- ✅ **No secrets in git history** (1.4 MB of diffs scanned: 0 hits for AWS/OpenAI/GitHub/Slack keys, private keys)
- ✅ **No secrets in config** — `prod.tfvars` explicitly delegates secrets to env vars; only `.env.example` committed
- ✅ **Parameterized SQL everywhere** — the one f-string column-name path (`relational_repo.py`) is safely constrained; user input never interpolated
- ✅ **Constant-time API-key comparison** (`secrets.compare_digest`) with clear docs
- ✅ **Docker hardening** — multi-stage builds, non-root user (uid 1001), `--no-install-recommends`, healthchecks, pinned `uv` version
- ✅ **Input validation** — query length ceiling + control-character stripping (`validate_search_query`)
- ✅ **Rate limiting designed in** (slowapi, global + per-route limits)
- ✅ **No `eval`/`exec`/`pickle`/`os.system`/shell-injection patterns** anywhere in the codebase
- ✅ **Dependency hygiene** — `uv.lock` fully pinned, Dependabot active (3 PRs open)

Findings below are ranked by severity. None are Critical.

---

## Findings

### HIGH

#### H1 — Silent partial-failure in ingest pipeline (integrity risk)
`pipelines/ingest/src/uk_regs_ingest/pipeline.py` contains **9 `except Exception` swallow-and-continue blocks** around every persistence step (document upsert L298, version L436, rules L474, change-set L509, corpus load L484, embeddings L454, LLM extraction L365, figure understanding L328, object store L130).

Consequences:
- An ingest can report **success while persisting nothing** (DB down → all writes skipped, each logged as `warning`).
- If the corpus load fails, **reconciliation is skipped** → a new document version is ingested *without change detection* → the system loses track of which rules changed between versions. For a regulatory-diff product this is a **data-accuracy failure**, not just a robustness issue.
- Embedding batch failure → rules persisted **without vectors** → hybrid search silently degrades to BM25-only for those rules, biasing retrieval results.

**Fix:** distinguish *resilient* failures (single LLM block — correct to continue) from *structural* failures (DB unavailable → abort run with non-zero exit + mark run failed in state store). Add a run-level success criteria: if `rules_persisted == 0 and rules_extracted > 0`, fail loudly. Add dead-letter/retry for transient DB/network errors (see IMPROVEMENTS.md).

#### H2 — `UK_REGS_BACKEND=auto` can silently serve Mock data in production
`apps/api/src/uk_regs_api/service.py::_select_backend` falls back based on availability ("Postgres if available" per `.env.example`). If Postgres is temporarily unreachable at backend construction, the API can start serving **`MockBackend` canned data** instead of erroring. For a compliance/regulatory context, silently returning fake rules is worse than a 503.

**Fix:** make fallback explicit — `auto` allowed only when `ENV=dev`; in prod, connection failure must crash-loop the task (ECS restarts it) rather than degrade to mock. Add a `/health` check that reports `backend: mock` as non-ready when `ENV != dev`.

#### H3 — Alembic migrations race at container boot (3 prod tasks × `--workers 2`)
`Dockerfile.api` CMD: `alembic upgrade head && uvicorn ...`. With `api_desired_count = 3` (prod.tfvars) all tasks run migrations **concurrently** against the same DB on every deploy → duplicate-DDL races, lock waits, or half-applied migrations under load.

**Fix:** move migrations to a separate one-shot ECS task / init container run before the service tasks start (Terraform `null_resource` or a `migrate` service with `desired_count=0` triggered in the deploy pipeline).

### MEDIUM

#### M1 — Rate limiting trusts client-supplied `X-Forwarded-For` and doesn't work across replicas
`apps/api/src/uk_regs_api/ratelimit.py`:
- `get_remote_addr()` takes the **first XFF hop** — trivially spoofable by clients sending a fake header, defeating per-IP limits (and enabling targeted DoS of other "IPs").
- The limiter is configured with slowapi's **own** `get_remote_addr` (peer IP), so behind the prod ALB *all* clients share the ALB's IP → one global bucket → legitimate users get rate-limited collectively. The custom XFF-aware function exists but isn't wired in — an integration bug.
- slowapi's default storage is **in-memory** → with 3 ECS tasks the effective limit is 3× the configured one and inconsistent per node.

**Fix:** use `ProxyHeadersMiddleware`/uvicorn `--proxy-headers` with a configured trusted-proxy count (or parse only the *last* untrusted XFF hop), and switch limiter storage to Redis for multi-replica correctness.

#### M2 — Read endpoints are permanently unauthenticated (by design, but risky in prod)
`auth.py`: GET/HEAD/OPTIONS bypass auth even when `API_KEY` is set — documented as "IDE read-only access". In the prod Terraform deployment (public ALB + Route53), this makes **the entire corpus, audit events, and change-set history world-readable**. `/docs` and `/openapi.json` are also exempted, publicly exposing the full API schema.

**Fix:** add a mode flag (e.g. `AUTH_MODE=read|write|all|off`). In prod set `AUTH_MODE=all` (key required for reads too); keep `read` for local IDE workflows. Gate `/docs`/`/openapi` behind auth or disable them via `FastAPI(openapi_url=None)` when `ENV=prod`.

#### M3 — Single static API key, no rotation/scoping
One `API_KEY` env var guards all mutations. No key id/versioning → rotation requires redeploy; no per-consumer keys → no attribution, no revocation of a single client, no audit binding of reviews to a caller.

**Fix:** move to a small key table (hashed keys + scopes + created_at + revoked_at) or AWS API Gateway usage plans / JWT with short-lived tokens. At minimum: store the key as a sha256 hash and compare digests, enabling future multi-key support.

#### M4 — SSRF surface in `HttpFetcher`
`packages/sourcing/src/uk_regs_sourcing/fetchers.py::_http_get` uses `httpx.get(url, follow_redirects=True)` with **no scheme/host allow-listing**, and `_scrape_html` follows any `href="*.pdf"` found in fetched HTML via `urljoin`. URLs come from operator-controlled `sources.yaml` (low initial risk), but a compromised/misconfigured watched page could redirect the fetcher to cloud metadata endpoints (`169.254.169.254`) or internal services.

**Fix:** validate final URL (after redirects) against an allow-list of publisher domains; block private/link-local CIDRs; pin `https` only; cap redirect hops; optionally route egress through the NAT gateway with an egress allow-list (already have NAT in prod — add NACL/SecurityGroup egress rules to publisher domains' IPs).

#### M5 — CI workflow lacks a `permissions` block
`.github/workflows/ci.yml` — no `permissions:` key. Depending on org defaults the `GITHUB_TOKEN` may inherit write scopes; a compromised action step could mutate the repo. Also `setup-uv` is pinned to `@v3` tag, not SHA.

**Fix:** add `permissions: { contents: read }` at workflow level; pin third-party actions to commit SHAs (`actions/checkout@<sha>`); consider `zizmor`/`actionlint` in CI.

### LOW

#### L1 — Dev compose credentials (`ukregs`/`minioadmin`/`password`) bound to host ports
`infra/compose/docker-compose.yml` publishes 5432/7474/7687/9000/9001 with well-known credentials. Fine on a dev box, dangerous if run on a shared host/VM. **Fix:** bind to `127.0.0.1:` prefixes.

#### L2 — Base images tracked by mutable tags
`python:3.12-slim`, `pgvector/pgvector:pg16`, `neo4j:5.20`, `minio/minio:latest`. `latest` for MinIO is especially unpinned. **Fix:** pin digests; add Renovate/Dependabot for Docker.

#### L3 — Auth prefix matching quirks
`_OPEN_PREFIXES` uses `startswith`, so any future route like `/health-admin` would be accidentally exempt; the "dummy compare" comment in `auth.py` isn't actually implemented (`provided is not None and ...` short-circuits). Both negligible today, cheap to fix.

#### L4 — No security-response headers / request body size cap
No middleware sets `X-Content-Type-Options`, and no explicit body-size limit beyond framework defaults (relevant for the JSON `review` POSTs). Low impact for a JSON API but free to add.

---

## Dependency snapshot (from `uv.lock`)

Core: fastapi 0.139.0, starlette 1.3.1, uvicorn 0.49.0, pydantic 2.13.4, sqlalchemy 2.0.51, psycopg 3.3.4, neo4j 6.2.0, boto3 1.43.40, httpx 0.28.1, openai 2.44.0, jinja2 3.1.6, cryptography 49.0.0.

Versions appear current (3 Dependabot PRs open at audit time). **Recommendation:** add `pip-audit` (or `uv audit`) as a CI step so CVE checks run on every PR rather than relying on Dependabot cadence — offline scan was not possible in this environment.

## Terraform notes

`prod.tfvars` is exemplary (secrets via `TF_VAR_*` env, multi-AZ, NAT, PITR backups). One thing to verify in `main.tf` (not fully audited here): ensure the ECS task definition passes secrets via `secrets:` (SSM/Secrets Manager ARNs) rather than `environment:` plain values.