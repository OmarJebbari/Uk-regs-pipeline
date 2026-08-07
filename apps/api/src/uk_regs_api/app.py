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

Ops Dashboard (Phase 5-3):
  GET /ops                -> live ops dashboard (HTML)
  GET /ops/stats          -> pipeline stats (JSON)
  GET /ops/stats?partial=1 -> HTMX partial fragment (HTML)

Auth: when API_KEY env is set, mutating routes require X-API-Key header.
Observability: structlog logging, OpenTelemetry tracing, Langfuse LLM tracing.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from uk_regs_export.renderers import (
    render_all,
    render_claude_md,
    render_consolidated_md,
    render_cursor_mdc,
    render_json,
)

from uk_regs_api.auth import ApiKeyMiddleware
from uk_regs_api.observability import setup_observability
from uk_regs_api.ratelimit import get_limiter, setup_rate_limiting, validate_search_query
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

# Wire observability (logging, tracing, request-id middleware) + auth + rate limits.
setup_observability(app)
setup_rate_limiting(app)
app.add_middleware(ApiKeyMiddleware)

# Lazy limiter reference for per-route decorators (None when slowapi absent).
_limiter = get_limiter()

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
def _search_impl(q: str, top_k: int, mode: str) -> dict:
    """Shared search body used by both the API route and the MCP server."""
    valid_modes = {"hybrid", "semantic", "keyword"}
    if mode not in valid_modes:
        raise HTTPException(
            status_code=400,
            detail=f"mode must be one of {sorted(valid_modes)}, got '{mode}'",
        )
    q = validate_search_query(q)
    backend = get_backend()
    hits: list[RuleHit] = backend.search(q, top_k=top_k, mode=mode)  # type: ignore[call-arg]
    return {
        "query": q,
        "mode": mode,
        "count": len(hits),
        "hits": [h.to_dict() for h in hits],
    }


@app.get("/search")
def search(
    request: Request,
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

    **Rate-limited** to ``RATELIMIT_SEARCH`` per IP. Query length capped at
    ``SEARCH_MAX_QUERY_CHARS``.
    """
    return _search_impl(q, top_k, mode)


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


def _log_audit(action: str, entity: str, entity_id: str, actor: str, payload: dict) -> None:
    """Record an audit event. Best-effort: never fail the request on audit errors."""
    try:
        from uk_regs_knowledge.repositories.relational_repo import AuditRepository

        AuditRepository().log(
            actor=actor, action=action, entity=entity, entity_id=entity_id, payload=payload
        )
    except Exception:
        # Audit logging is best-effort — must not break the user's action.
        pass


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
    Records the decision in the append-only audit trail.
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
    _log_audit(decision, "change_set", change_set_id, reviewer, {"notes": notes})
    return changeset_to_dict(cs)


# --------------------------------------------------------------------------- #
#  Export — IDE-native context files (Cursor .mdc, CLAUDE.md, JSON)
# --------------------------------------------------------------------------- #
def _get_export_rules():
    """Fetch current rules once; cached per-request by the backend."""
    backend = get_backend()
    return backend.get_current()


@app.get("/export/markdown")
def export_markdown() -> dict:
    """Backwards-compatible: consolidated Markdown as a JSON payload."""
    rules = _get_export_rules()
    _, content = render_consolidated_md(rules)
    return {
        "filename": "CONSOLIDATED_REGULATIONS.md",
        "content": content,
        "rule_count": len(rules),
    }


@app.get("/export/bundle")
def export_bundle() -> dict:
    """
    Manifest of every file the export can generate.

    Returns ``{files: [{path, size}], rule_count}`` so a client (CLI, CI job,
    or the ops dashboard) knows exactly what artifacts are available for
    download via the granular endpoints below.
    """
    rules = _get_export_rules()
    bundle = render_all(rules)
    return {
        "rule_count": len(rules),
        "files": [{"path": p, "size": len(c)} for p, c in sorted(bundle.items())],
    }


@app.get("/export/consolidated.md", response_class=PlainTextResponse)
def export_consolidated_download() -> PlainTextResponse:
    """Download the full consolidated regulations as ``.md``."""
    _, content = render_consolidated_md(_get_export_rules())
    return PlainTextResponse(content, media_type="text/markdown")


@app.get("/export/claude.md", response_class=PlainTextResponse)
def export_claude_download() -> PlainTextResponse:
    """Download ``CLAUDE.md`` — the repo-root brief for Claude Code/Desktop."""
    _, content = render_claude_md(_get_export_rules())
    return PlainTextResponse(content, media_type="text/markdown")


@app.get("/export/regulations.json")
def export_json_download() -> dict:
    """Download the machine-readable JSON export of every current rule."""
    _, content = render_json(_get_export_rules())
    import json

    return json.loads(content)


@app.get("/export/cursor/{category}.mdc", response_class=PlainTextResponse)
def export_cursor_mdc(category: str) -> PlainTextResponse:
    """
    Download a single Cursor ``.mdc`` rules file for a given category.

    Returns 404 if the category has no rules (and thus no generated file).
    """
    bundle = render_cursor_mdc(_get_export_rules())
    key = f".cursor/rules/{category}.mdc"
    if key not in bundle:
        valid = sorted({k.split("/")[-1].removesuffix(".mdc") for k in bundle})
        raise HTTPException(
            status_code=404,
            detail=f"No .mdc for category '{category}'. Available: {valid}",
        )
    return PlainTextResponse(bundle[key], media_type="text/markdown")


@app.get("/export/cursorrules", response_class=PlainTextResponse)
def export_cursorrules_download() -> PlainTextResponse:
    """Download the legacy flat ``.cursorrules`` file."""
    bundle = render_cursor_mdc(_get_export_rules())
    return PlainTextResponse(bundle.get(".cursorrules", ""), media_type="text/markdown")


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
    _log_audit(decision, "change_set", change_set_id, reviewer, {"notes": notes, "ui": "htmx"})
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


# =========================================================================== #
#  Ops Dashboard (Phase 5-3)
# =========================================================================== #
def _enrich_stats_with_sources(stats) -> None:
    """Best-effort: fill in source counts from the sourcing registry."""
    try:
        from uk_regs_sourcing.registry import load_registry

        registry = load_registry()
        stats.sources_count = len(registry.sources)
        stats.sources_enabled = sum(1 for s in registry.sources if s.enabled)
    except Exception:
        # Sourcing subsystem not available — leave counts at 0.
        pass


@app.get("/ops", response_class=HTMLResponse)
def ops_dashboard(request: Request) -> HTMLResponse:
    """Full ops dashboard page (HTML)."""
    backend = get_backend()
    stats = backend.get_ops_stats()
    _enrich_stats_with_sources(stats)
    return templates.TemplateResponse(request, "ops.html", {"stats": stats})


@app.get("/ops/stats")
def ops_stats(partial: bool = Query(False, description="Return HTML partial for HTMX")):
    """
    Pipeline statistics snapshot.

    Without ``?partial=1`` → JSON (for programmatic consumers / curl).
    With ``?partial=1`` → rendered HTML fragment (for HTMX dashboard refresh).
    """
    backend = get_backend()
    stats = backend.get_ops_stats()
    _enrich_stats_with_sources(stats)

    if partial:
        # Jinja2Templates needs a Request; for the partial we render via env.
        html = templates.env.get_template("ops_partial.html").render(stats=stats)
        return HTMLResponse(content=html)
    return stats.to_dict()


# =========================================================================== #
#  Audit trail (Phase §11)
# =========================================================================== #
@app.get("/audit")
def list_audit(
    entity: str | None = Query(None, description="Filter by entity (change_set, rule, document)"),
    entity_id: str | None = Query(None, description="Filter by entity id"),
    actor: str | None = Query(None, description="Filter by actor (user/system)"),
    action: str | None = Query(None, description="Filter by action (ingest, apply, reject, …)"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict:
    """
    Query the append-only audit trail (newest-first).

    Every mutating action (ingest, review, export) is recorded here, giving a
    complete, queryable history of who changed what, when, and why — essential
    for a compliance-critical system.
    """
    backend = get_backend()
    events = backend.list_audit_events(
        entity=entity,
        entity_id=entity_id,
        actor=actor,
        action=action,
        limit=limit,
        offset=offset,
    )
    return {"count": len(events), "events": events}


@app.get("/ui/audit", response_class=HTMLResponse)
def ui_audit(
    request: Request,
    entity: str | None = Query(None),
    action: str | None = Query(None),
) -> HTMLResponse:
    """Audit trail page (HTML) — filterable history of all mutations."""
    backend = get_backend()
    events = backend.list_audit_events(entity=entity, action=action, limit=200)
    return templates.TemplateResponse(
        request,
        "audit.html",
        {"events": events, "filter_entity": entity, "filter_action": action},
    )
