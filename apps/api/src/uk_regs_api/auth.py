"""
API-key authentication middleware.

When ``API_KEY`` is set in the environment, mutating routes (POST/PUT/DELETE)
require a matching ``X-API-Key`` header. Read-only GET routes stay open for
the IDE/MCP client. When ``API_KEY`` is unset, auth is bypassed (dev mode).

This implements **Option 1** from the Phase 4B-3b plan: gate only writes.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from uk_regs_core import get_settings

# Routes exempt from auth even when API_KEY is set
_OPEN_PREFIXES = ("/health", "/healthz", "/readyz", "/docs", "/openapi", "/redoc")


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """
    Enforce ``X-API-Key`` on mutating methods when API_KEY is configured.

    - GET/HEAD/OPTIONS → always allowed (read-only IDE/MCP access)
    - POST/PUT/PATCH/DELETE → require valid key
    - ``/health*``, ``/docs``, ``/openapi`` → always allowed
    """

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        settings = get_settings()

        # Bypass entirely when no key is configured (dev mode)
        if not settings.api_key:
            return await call_next(request)

        path = request.url.path

        # Health/docs endpoints always open
        if any(path.startswith(p) for p in _OPEN_PREFIXES):
            return await call_next(request)

        # Read-only methods always allowed
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return await call_next(request)

        # Mutating request: validate the key
        provided = request.headers.get("x-api-key") or request.headers.get("X-API-Key")
        if provided != settings.api_key:
            return JSONResponse(
                status_code=401,
                content={"detail": "invalid or missing API key"},
            )

        return await call_next(request)