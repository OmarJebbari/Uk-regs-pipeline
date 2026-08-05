"""
FastAPI app — the context-serving API (Phase 3 + 4B-3a Review UI + 4B-3b Auth/Observability).

JSON API:
  GET /health                   -> liveness (process up)
  GET /healthz                  -> k8s liveness probe
  GET /readyz                   -> k8s readiness probe (backend reachable)
  GET /search?q=...&top_k=10
  GET /rules/{rule_id}
  GET /current?topic=&category=
  GET /figures/{rule_id}
  GET /diff/{rule_id}
  GET /changes?status=&limit=
  GET /changes/{change_set_id}
  POST /changes/{change_set_id}/review   (decision, reviewer, notes)  [auth]
  GET /export/markdown        -> consolidated .cursor/rules + CLAUDE.md export

Review UI (Jinja2 + HTMX, dark professional theme):
  GET /ui/                            -> dashboard of pending changes
  GET /ui/changes/{change_set_id}     -> detail with side-by-side diff

Auth: when API_KEY env is set, mutating routes require X-API-Key header.
Observability: structlog logging, OpenTelemetry tracing, Langfuse LLM tracing.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from uk_regs_api.auth import ApiKeyMiddleware
from uk_regs_api.observability import setup_observability
from uk_regs_api.service import (
    RuleHit,
    changeset_to_dict,
    changesets_to_dicts,
    get_backend,
    rules_to_dicts,
)

app = FastAPI(
    title="UK Regs Context API",
    description=(
        "Semantic search + current-rule + diff + figures + changelog for UK "
        "building regulations. Every response carries citations."
    ),
    version="0.5.0",
)

# Wire observability (logging, tracing, request-id middleware) + auth.
setup_observability(app)
app.add_middleware(ApiKeyMiddleware)

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _fmt_dt(value: datetime | None) -> str:
    if not value:
        return "—"
    try:
        return value.strftime("%Y-%m-%d %H:%M UTC")
    except AttributeError:
        return str(value)


templates.env.filters["fmt_dt"] = _fmt_dt


# --------------------------------------------------------------------------- #
#  Health probes (liveness + readiness)
# --------------------------------------------------------------------------- #
@app.get("/health")
@app.get("/healthz")
def health() -> dict[str, str]:
    """Liveness probe — process is up and serving."""
    return {"status": "ok"}


@app.get("/readyz")
def readyz(response: Response) -> dict:
    """
    Readiness probe — backend is reachable and queryable.

    Returns 200 when the backend responds, 503 otherwise. k8s/docker route
    traffic to this instance only when this returns 200.
    """
    checks: dict[str, str] = {}
    ok = True
    try:
        backend = get_backend()
        # cheap call: count current rules (works for both Mock + Postgres)
        rules = backend.get_current()
        checks["backend"] = f"ok ({len(rules)} rules)"
    except Exception as exc:  # noqa: BLE001
        ok = False
        checks["backend"] = f"fail: {exc.__class__.__name__}: {exc}"
    response.status_code = 200 if ok else 503
    return {"ready": ok, "checks": checks}


# --------------------------------------------------------------------------- #
#  Search
# --------------------------------------------------------------------------- #
@app.get("/search")
def search(
    q: str = Query(..., description="Natural-language query, e.g. 'fire door rating'"),
    top_k: int = Query(10, ge=1, le=50),
    mode: str = Query(
        "hybrid",
        description="hybrid (BM25 + vector via RRF, default) | semantic "
        "(vector only) | keyword (BM25 only)",
    ),
) -> dict:
    """
    Hybrid search over current active rules (Phase 5-2).

    ``mode`` controls the retrieval strategy:
      - **hybrid** *(default)*: fuses BM25 keyword + vector semantic results
        via Reciprocal Rank Fusion. Best recall.
      - **semantic**: vector cosine similarity only. Best for paraphrases /
        concept queries.
      - **keyword**: BM25 full-text only. Best for exact-term / identifier
        queries (e.g. "FD30", "BS 476-22").

    Falls back to keyword if embeddings are unavailable, so search never
    hard-fails.
    """
    valid_modes = {"hybrid", "semantic", "keyword"}
    if mode not in valid_modes:
        raise HTTPException(
            status_code=400,
            detail=f"mode must be one of {sorted(valid_modes)}, got '{mode}'",
        )
    backend = get_backend()
    hits: list[RuleHit] = backend.search(q, top_k=top_k, mode=mode)  # type: ignore[call-arg]
    return {
        "query": q,
        "mode": mode,
        "count": len(hits),
        "hits": [h.to_dict() for h in hits],
    }


# --------------------------------------------------------------------------- #
#  Single rule
# --------------------------------------------------------------------------- #
@app.get("/rules/{rule_id}")
def get_rule(rule_id: str) -> dict:
    backend = get_backend()
    rule = backend.get_rule(rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail=f"rule '{rule_id}' not found")
    return rules_to_dicts([rule])[0]


# --------------------------------------------------------------------------- #
#  Current rules (the 'current truth')
# --------------------------------------------------------------------------- #
@app.get("/current")
def get_current(
    topic: str | None = Query(None, description="Free-text topic filter"),
    category: str | None = Query(
        None,
        description="One of: fire, structure, ventilation, access, "
        "drainage, energy, sound, safety, other",
    ),
) -> dict:
    backend = get_backend()
    rules = backend.get_current(topic=topic, category=category)
    return {
        "topic": topic,
        "category": category,
        "count": len(rules),
        "rules": rules_to_dicts(rules),
    }


# --------------------------------------------------------------------------- #
#  Figures
# --------------------------------------------------------------------------- #
@app.get("/figures/{rule_id}")
def get_figure(rule_id: str) -> dict:
    backend = get_backend()
    fig = backend.get_figure(rule_id)
    if fig is None:
        return {
            "rule_id": rule_id,
            "figure": None,
            "note": "Figure retrieval lands in Phase 4 (VLM + object store).",
        }
    return {"rule_id": rule_id, "figure": fig}


# --------------------------------------------------------------------------- #
#  Diff (what changed for this rule)
# --------------------------------------------------------------------------- #
@app.get("/diff/{rule_id}")
def diff(rule_id: str) -> dict:
    backend = get_backend()
    return backend.diff(rule_id)


# --------------------------------------------------------------------------- #
#  Change log
# --------------------------------------------------------------------------- #
@app.get("/changes")
def list_changes(
    status: str | None = Query(None, description="proposed|validated|applied|rejected|rolled_back"),
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    backend = get_backend()
    css = backend.list_changes(status=status, limit=limit)
    return {"count": len(css), "change_sets": changesets_to_dicts(css)}


@app.get("/changes/{change_set_id}")
def get_change_set(change_set_id: str) -> dict:
    """Fetch a single change-set with all its deltas for review."""
    backend = get_backend()
    cs = backend.get_change_set(change_set_id)
    if cs is None:
        raise HTTPException(status_code=404, detail=f"change-set '{change_set_id}' not found")
    return changeset_to_dict(cs)


# --------------------------------------------------------------------------- #
#  Review (human-in-the-loop gate)
# --------------------------------------------------------------------------- #
_VALID_DECISIONS = {"applied", "rejected", "rolled_back"}


@app.post("/changes/{change_set_id}/review")
def review_change_set(
    change_set_id: str,
    decision: str = Form(..., description="applied | rejected | rolled_back"),
    reviewer: str = Form(...),
    notes: str = Form(""),
) -> dict:
    """
    Apply a human review decision to a change-set.

    Returns the updated change-set. Used by both the JSON API and the HTMX UI.
    """
    if decision not in _VALID_DECISIONS:
        raise HTTPException(
            status_code=400,
            detail=f"decision must be one of {sorted(_VALID_DECISIONS)}, got '{decision}'",
        )
    backend = get_backend()
    cs = backend.review_change_set(change_set_id, decision, reviewer, notes)
    if cs is None:
        raise HTTPException(status_code=404, detail=f"change-set '{change_set_id}' not found")
    return changeset_to_dict(cs)


# --------------------------------------------------------------------------- #
#  Export — consolidated Markdown for IDE rules (Cursor/Claude/Copilot)
# --------------------------------------------------------------------------- #
@app.get("/export/markdown")
def export_markdown() -> dict:
    """Generate .cursor/rules + CLAUDE.md content from the current truth."""
    backend = get_backend()
    rules = backend.get_current()
    lines = [
        "# UK Building Regulations — Current Context",
        "",
        f"_Auto-generated from the UK Regs pipeline. {len(rules)} active rules._",
        "",
        "---",
        "",
    ]
    for r in rules:
        lines.append(f"## {r.title or r.rule_id}")
        lines.append(f"- **Rule ID:** `{r.rule_id}`")
        lines.append(f"- **Section:** `{r.section_path}`")
        lines.append(f"- **Category:** {r.category.value}")
        lines.append(f"- **Status:** {r.status.value} (valid from {r.valid_from})")
        lines.append("")
        lines.append(f"> {r.text}")
        lines.append("")
        if r.parameters:
            lines.append("**Parameters:**")
            for k, v in r.parameters.items():
                lines.append(f"- `{k}` = `{v}`")
            lines.append("")
        if r.references:
            lines.append(f"**References:** {', '.join(r.references)}")
            lines.append("")
        c = r.citation
        lines.append(f"**Source:** {c.doc_id} {c.version}, p.{c.page} — _\u201c{c.raw_text}\u201d_")
        lines.append("")
        lines.append("---")
        lines.append("")
    return {
        "filename": "CONSOLIDATED_REGULATIONS.md",
        "content": "\n".join(lines),
        "rule_count": len(rules),
    }


# =========================================================================== #
#  Source monitoring (Phase 5-1)
# =========================================================================== #
@app.get("/sources")
def list_sources() -> dict:
    """
    List configured sources and their last-poll status.

    Reads from the source registry + the ``source_poll_state`` table (or the
    in-memory store in dev). Use this to monitor ingestion health.
    """
    try:
        from uk_regs_sourcing.registry import load_registry
        from uk_regs_sourcing.state import get_state_store

        registry = load_registry()
        store = get_state_store()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=503,
            detail=f"sourcing subsystem unavailable: {exc.__class__.__name__}",
        ) from exc

    out = []
    for s in registry.sources:
        last = store.last_poll(s.source_id)
        seen = store.get_seen(s.source_id)
        out.append(
            {
                "source_id": s.source_id,
                "title": s.title,
                "publisher": s.publisher,
                "fetcher": s.fetcher,
                "enabled": s.enabled,
                "poll_interval_minutes": s.poll_interval_minutes,
                "doc_type": s.doc_type,
                "documents_seen": len(seen),
                "last_polled_at": last.isoformat() if last else None,
            }
        )
    return {"count": len(out), "sources": out}


@app.post("/sources/poll")
def trigger_poll(
    force: bool = Query(False, description="Ignore poll intervals"),
    apply: bool = Query(False, description="Auto-apply change-sets"),
) -> dict:
    """
    Trigger a manual poll cycle (synchronous). Auth-protected via API key.

    Returns a summary of what was ingested. Intended for ops dashboards and
    "I just dropped a PDF, ingest it now" workflows.
    """
    try:
        from uk_regs_sourcing.scheduler import run_once
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=503,
            detail=f"sourcing subsystem unavailable: {exc.__class__.__name__}",
        ) from exc

    cycle = run_once(force=force, apply=apply)
    return {
        "started_at": cycle.started_at.isoformat(),
        "finished_at": cycle.finished_at.isoformat() if cycle.finished_at else None,
        "total_ingested": cycle.total_ingested,
        "sources": [
            {
                "source_id": s.source_id,
                "advertised": s.advertised,
                "new": s.new,
                "updated": s.updated,
                "skipped": s.skipped,
                "failed": s.failed,
                "errors": s.errors,
            }
            for s in cycle.sources
        ],
    }


# =========================================================================== #
#  Review UI — Jinja2 + HTMX (dark professional theme)
# =========================================================================== #
_DECISION_STYLES = {
    "match": ("badge-match", "Match"),
    "partial": ("badge-partial", "Amended"),
    "novel": ("badge-novel", "New"),
    "conflict": ("badge-conflict", "Conflict"),
    "supersede": ("badge-supersede", "Superseded"),
}
_STATUS_STYLES = {
    "proposed": "status-proposed",
    "validated": "status-validated",
    "applied": "status-applied",
    "rejected": "status-rejected",
    "rolled_back": "status-rolled-back",
}


@app.get("/ui/", response_class=HTMLResponse)
def ui_dashboard(request: Request) -> HTMLResponse:
    """Dashboard: review queue with stats."""
    backend = get_backend()
    all_cs = backend.list_changes(limit=100)
    counts = {
        "proposed": sum(1 for c in all_cs if c.status == "proposed"),
        "applied": sum(1 for c in all_cs if c.status == "applied"),
        "rejected": sum(1 for c in all_cs if c.status == "rejected"),
        "rolled_back": sum(1 for c in all_cs if c.status == "rolled_back"),
        "total": len(all_cs),
    }
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "change_sets": all_cs,
            "counts": counts,
            "decision_styles": _DECISION_STYLES,
            "status_styles": _STATUS_STYLES,
        },
    )


@app.get("/ui/changes/{change_set_id}", response_class=HTMLResponse)
def ui_change_detail(change_set_id: str, request: Request) -> HTMLResponse:
    """Detail view: side-by-side deltas with review form."""
    backend = get_backend()
    cs = backend.get_change_set(change_set_id)
    if cs is None:
        raise HTTPException(status_code=404, detail="change-set not found")
    # Fetch the new + matched rules for side-by-side display
    rules = []
    for d in cs.deltas:
        new_rule = backend.get_rule(d.new_rule_id)
        matched_rule = backend.get_rule(d.matched_rule_id) if d.matched_rule_id else None
        rules.append({"delta": d, "new_rule": new_rule, "matched_rule": matched_rule})
    return templates.TemplateResponse(
        request,
        "detail.html",
        {
            "cs": cs,
            "rules": rules,
            "decision_styles": _DECISION_STYLES,
            "status_styles": _STATUS_STYLES,
        },
    )


@app.post("/ui/changes/{change_set_id}/review", response_class=HTMLResponse)
def ui_review_submit(
    change_set_id: str,
    request: Request,
    decision: str = Form(...),
    reviewer: str = Form(...),
    notes: str = Form(""),
) -> HTMLResponse:
    """HTMX endpoint: apply review then re-render the detail page."""
    backend = get_backend()
    backend.review_change_set(change_set_id, decision, reviewer, notes)
    cs = backend.get_change_set(change_set_id)
    if cs is None:
        raise HTTPException(status_code=404, detail="change-set not found")
    rules = []
    for d in cs.deltas:
        new_rule = backend.get_rule(d.new_rule_id)
        matched_rule = backend.get_rule(d.matched_rule_id) if d.matched_rule_id else None
        rules.append({"delta": d, "new_rule": new_rule, "matched_rule": matched_rule})
    return templates.TemplateResponse(
        request,
        "detail.html",
        {
            "cs": cs,
            "rules": rules,
            "decision_styles": _DECISION_STYLES,
            "status_styles": _STATUS_STYLES,
            "flash": f"Change-set marked as '{decision}'.",
        },
    )
