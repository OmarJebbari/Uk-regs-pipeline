# Figures & Diagram Handling — Audit

> Status: **Audit complete — 3 blocking gaps, 2 enhancements identified.**
> Scope: end-to-end figure pipeline (PDF rasterisation → object store → VLM understanding → rule linking → API retrieval).

## 1. Current flow

```
PDF parse (pdf_parser._render_block_image)
   │  rasterises image-block bbox at page_dpi → PNG
   │  keys: figures/{doc_id}/{version}/{ulid}.png
   ▼
ParsedDocument.figure_images: dict[key, bytes]
   │
   ▼
pipeline._drain_figures()  →  object_store.put(key, png)   ✅ works
   │
   ▼
pipeline._understand_figures()  →  FigureUnderstander.describe(key)   ❌ see GAP-1
   │
   ▼
FigureRepository.upsert()  →  figures table (illustrates_rules[])   ⚠️  see GAP-2
   │
   ▼
GET /figures/{rule_id}  →  FigureRepository.list_for_rule()   ✅ works
```

## 2. What works

| Stage | Component | Status |
|-------|-----------|--------|
| Detection | `pdf_parser` identifies `_IMAGE_BLOCK` blocks and extracts bbox | ✅ |
| Rasterisation | `_render_block_image` renders at configurable DPI | ✅ |
| Storage | `_drain_figures` uploads PNGs to S3/MinIO | ✅ |
| Schema | `figures` table with `illustrates_rules` array + `image_key` | ✅ |
| API | `GET /figures/{rule_id}` queries via `list_for_rule()` | ✅ |
| VLM prompt | `VLM_SYSTEM_PROMPT` explicitly forbids inferring unwritten values | ✅ |

## 3. Blocking gaps

### GAP-1: `_understand_figures` calls a non-existent method

**Location:** `pipelines/ingest/src/uk_regs_ingest/pipeline.py`

```python
desc = understander.describe(fb.figure_key)  # type: ignore[arg-type]
```

`FigureUnderstander` has **no `describe()` method**. The class exposes `understand()`, which takes `image_bytes` (not a key) and returns a full `Figure` object (not a string). This code is dead — it would raise `AttributeError` if `skip_llm=False` and a figure block were present.

**Impact:** Figures are rasterised and stored, but **never understood**. The VLM stage silently no-ops (caught by the bare `except Exception`).

**Fix:** Replace with the correct call. The pipeline must:
1. Fetch the PNG bytes from the object store by `fb.figure_key`.
2. Call `understander.understand(image_bytes=..., doc_id=..., version=..., page=..., caption=...)`.
3. Set `image_key` on the returned `Figure` to `fb.figure_key` (currently hardcoded to `""`).
4. Upsert the `Figure` via `FigureRepository`.

---

### GAP-2: Figures are never linked to rules (`illustrates_rules` is empty)

**Location:** `FigureUnderstander.understand()` and `pipeline._understand_figures()`

The `Figure` model has `illustrates_rule_ids: list[str]`, but:
- `understand()` always sets `illustrates_rule_ids=[]`.
- `_understand_figures()` returns `list[dict]` of `{block_id, description}` — the result is **never persisted** to the `figures` table and never linked to any `rule_id`.

**Impact:** `GET /figures/{rule_id}` will always return `null`, because no figure's `illustrates_rules` array is ever populated.

**Fix:** After understanding a figure, run a linking step:
1. For each figure, take its surrounding text blocks (caption + ±2 reading-order neighbours).
2. Find rules extracted from the same page whose `normalized_form` or `references` overlap with the figure's `elements` (labels/dimensions).
3. Populate `illustrates_rule_ids` with matched `rule_id`s.
4. Upsert the figure with `FigureRepository.upsert()`.

---

### GAP-3: `image_key` is hardcoded to empty string

**Location:** `FigureUnderstander.understand()`

```python
return Figure(
    ...
    image_key="",  # set by storage layer on upload   ← never actually set
    ...
)
```

The comment says "set by storage layer", but `_drain_figures` uploads by key and never touches the `Figure` object. The object-store key (`figures/{doc_id}/{version}/{ulid}.png`) is known at parse time and should flow through.

**Impact:** Even if figures were understood and persisted, the `image_key` would be blank — the IDE could never retrieve the raw image.

**Fix:** Pass `image_key` into `understand()` as a parameter (it's already available as `fb.figure_key` in the pipeline).

## 4. Enhancements (non-blocking)

### ENH-1: VLM numeric grounding (mirror the text-side `_ground_parameters`)

The VLM extracts `elements[].value` (dimensions like "1200"). Currently these are **not** validated against the source. A VLM hallucination like "12000 mm" would pass unchecked.

**Recommendation:** After VLM extraction, cross-check each `element.value` against the deterministic measurements from the same page. Drop ungrounded values and reduce the figure's effective confidence. This mirrors the text-side `_ground_parameters` logic added in the rule extractor.

### ENH-2: Figure→rule confidence score

When linking figures to rules (GAP-2 fix), emit a `link_confidence` based on:
- Caption keyword overlap with rule `normalized_form`.
- Shared page + reading-order proximity.
- Shared standards references.

Store this in a `figure_rule_links` join table (or as a JSON column) so the IDE can filter "show me diagrams I'm ≥80% sure illustrate this rule."

## 5. Recommended action plan

| Priority | Item | Effort |
|----------|------|--------|
| **P0** | Fix GAP-1: wire `_understand_figures` to `FigureUnderstander.understand()` | S |
| **P0** | Fix GAP-3: pass `image_key` through instead of `""` | XS |
| **P0** | Fix GAP-2: implement figure→rule linking + persist via `FigureRepository` | M |
| P1 | ENH-1: VLM numeric grounding | S |
| P2 | ENH-2: figure→rule link confidence | M |

## 6. Test coverage

There are currently **no tests** for:
- `_understand_figures` (it's dead code, so untestable as-is).
- `FigureUnderstander.understand()` with a real image.
- Figure→rule linking (doesn't exist yet).

After fixing the gaps, add:
- Unit test: mock VLM returns a `Figure`, verify `image_key` is set and `FigureRepository.upsert` is called.
- Integration test: feed a sample PDF with a figure (see `scripts/make_sample_pdf.py`), verify the figure is understood, persisted, and linked to a rule on the same page.
- Grounding test: mock VLM emits a hallucinated dimension, verify it's dropped.