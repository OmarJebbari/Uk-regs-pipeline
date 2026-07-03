"""FastAPI app — minimal health endpoint (Phase 3 adds full routes)."""

from __future__ import annotations

from fastapi import FastAPI

app = FastAPI(
    title="UK Regs Context API",
    description="Semantic search + current-rule + diff + figures for UK building regulations.",
    version="0.1.0",
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}