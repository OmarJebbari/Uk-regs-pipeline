"""
FastAPI context-serving API — entrypoint stub.

Phase 3 will populate real endpoints (search_regulations, get_current, diff,
figures, check_design_intent). For now this provides a health endpoint so the
app is importable and runnable.
"""

from __future__ import annotations


def main() -> None:
    import uvicorn

    uvicorn.run("uk_regs_api.app:app", host="0.0.0.0", port=8000, reload=True)


if __name__ == "__main__":
    main()
