# Limitations & Known Gaps

> Honest inventory of what this pipeline does well, where it cuts corners, and
> what is explicitly out of scope. Updated as gaps are closed.

---

## 1. Critical gaps (affect correctness)

### 1.1 Figures are never understood or linked (P0)

See [`docs/figures-audit.md`](figures-audit.md) for full detail. Summary:
- `_understand_figures()` in the ingest pipeline calls `FigureUnderstander.describe()`, a method that **does not exist**.
- The VLM stage is therefore dead code; figures are rasterised and stored but never interpreted.
- `illustrates_rule_ids` is always `[]`, so `GET /figures/{rule_id}` always returns `null`.

**Status:** Audit complete; fix not yet implemented.

### 1.2 No temporal deduplication of identical re-published PDFs

If a source publishes the same PDF twice (same content, different URL), the
pipeline will re-ingest it fully and create a new version. There is no
content-hash check that short-circuits: "this is byte-identical to version
N-1, skip."

**Impact:** Wasted LLM cost and spurious "no-change" change-sets.

**Mitigation:** Add a SHA-256 check in the parser stage against the
`documents` table before LLM extraction.

---

## 2. Accuracy limitations (affect precision, not correctness)

### 2.1 LLM numeric grounding is recall-biased

The `_ground_parameters` function (see `rule_extractor.py`) validates LLM-emitted
numbers against **every** number in the source text — not against the specific
sentence. A rule saying "minimum 30 minutes" will be accepted if "30" appears
*anywhere* on the page, even in an unrelated context.

**Why:** Precise span-level grounding requires the LLM to emit source spans
(e.g., Start/End offsets), which adds complexity and latency.

**Impact:** Low risk in practice (regulation pages rarely have unrelated
numbers that collide), but a known recall-vs-precision trade-off.

### 2.2 Deterministic extractors miss non-physical quantities

The regex-based extractors (`deterministic/extractors.py`) capture physical
units (mm, m², kN, MPa…) but **not**:
- Time values ("30 minutes", "2 hours")
- Counts ("3 storeys", "2 exits")
- Angles ("45°" — unless written as "deg")

The `_ground_parameters` numeric-scan backstops this for *validation*, but the
typed `parameters` dict will only carry physical measurements. The LLM may
emit time/count parameters, but they won't have the high-precision deterministic
anchor.

### 2.3 VLM descriptions are not numerically grounded

Unlike the text-side extractor, the VLM (`figures/understander.py`) extracts
`elements[].value` (dimensions like "1200") with **no** validation against
deterministic facts. A hallucinated "12000 mm" passes unchecked.

**Status:** Identified as ENH-1 in the figures audit; not yet implemented.

### 2.4 Confidence scores are heuristically composed, not calibrated

`Rule.confidence` is a composite of the LLM's self-reported confidence minus
penalties for ungrounded values. It is **not** calibrated against a held-out
test set — a score of 0.85 does not mean "85% likely correct."

The activation threshold (`ACTIVATION_THRESHOLD`, default 0.85) was chosen by
inspection, not by optimising for F1 on a labelled corpus.

---

## 3. Scalability limitations

### 3.1 Per-block LLM calls (no batching)

Each content block triggers a separate LLM call. A 200-page document with
~600 blocks = 600 API calls. There is no prompt batching or multi-block
extraction in a single request.

**Impact:** High latency (minutes per document) and cost (each call pays
system-prompt token overhead).

**Mitigation:** Future work — batch N blocks per call with a list response model.

### 3.2 No incremental / streaming extraction

The pipeline processes a full document atomically. There is no checkpointing
mid-document; a crash at block 400 of 600 loses all work (the retry layer
helps, but a process restart loses in-memory state).

### 3.3 Embedding re-computation on re-ingest

Rules are re-embedded on every ingest even if their `normalized_form` is
unchanged. The `reconcile` layer detects *semantic* changes but embeddings are
recomputed upstream regardless.

---

## 4. Operational limitations

### 4.1 LLM cost estimation is approximate

The `_COST_PER_1K` table in `client.py` uses publicly listed pricing snapshots.
Real costs vary by:
- Provider tier / volume discounts
- Region
- Prompt caching (where available)

The logged `llm_cost_usd` is a **rough** estimate for dashboards, not for
invoicing or budget enforcement.

### 4.2 No rate-limit / quota handling

The `tenacity` retry layer retries on transient errors but does **not**
respect `Retry-After` headers or implement token-bucket throttling. Under
heavy load, the pipeline may hit provider rate limits and retry-storm.

### 4.3 Object store is treated as always-available

If the object store (S3/MinIO) is down, figure images and source PDFs are
silently skipped (`logger.warning("object store unavailable")`). There is no
dead-letter queue or retry-on-store-failure mechanism.

---

## 5. Security & compliance limitations

### 5.1 PDFs are parsed in-process (no sandbox)

PDF parsing uses PyMuPDF in the same process as the pipeline. A maliciously
crafted PDF could exploit parser vulnerabilities. For untrusted sources,
consider parsing in an isolated container or gVisor sandbox.

### 5.2 No PII redaction

Building regulations are public documents, but if the pipeline is ever pointed
at internal documents (e.g., design reviews), there is no PII redaction layer.
All extracted text is sent to the LLM provider verbatim.

### 5.3 API key auth is static (no rotation / OAuth)

The API (`apps/api`) uses a single static `API_KEY` for mutating endpoints.
There is no key rotation, per-client keys, or OAuth flow. Suitable for
internal/single-tenant use; not for multi-tenant.

---

## 6. Out of scope (by design)

These are **not** limitations — they are deliberate non-goals:

| Item | Rationale |
|------|-----------|
| PDF form-field extraction | UK Building Regs PDFs are flat, not interactive forms |
| OCR of scanned documents | Source PDFs are born-digital (text layer present) |
| Multi-language support | UK regulations are English-only |
| Real-time ingestion | Batch ingestion via polling is sufficient; regulations change infrequently |
| IDE plugin packaging | The API is the integration point; IDE plugins are the consumer's responsibility |

---

## 7. Change log

| Date | Change |
|------|--------|
| 2026-08-11 | Initial limitations inventory created alongside figures audit + grounding work |