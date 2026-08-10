"""
Rate limiting + input validation middleware (slowapi).

Two layers of protection:
  1. **Global per-IP limit** — a catch-all to protect every endpoint from
     runaway clients / DDoS. Configurable via ``RATELIMIT_GLOBAL``.
  2. **Route-specific limits** — tighter ceilings on expensive endpoints
     (``/search`` runs vector + BM25; mutations hit the DB). Applied via
     the ``@limiter.limit`` decorator.

Also exports a reusable ``validate_search_query`` helper that enforces
the ``SEARCH_MAX_QUERY_CHARS`` ceiling and rejects control characters.
"""

from __future__ import annotations

from uk_regs_core import get_settings


def get_remote_addr(request) -> str:
    """
    Resolve the client IP for rate-limit keying.

    Honours ``X-Forwarded-For`` (first hop) so limits work correctly
    behind a load balancer / ingress. Falls back to the direct peer.
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# --------------------------------------------------------------------------- #
#  Limiter singleton (lazily created so tests don't require slowapi installed)
# --------------------------------------------------------------------------- #
_limiter = None


def get_limiter():
    """Return the process-wide slowapi Limiter, creating it on first call."""
    global _limiter
    if _limiter is not None:
        return _limiter

    try:
        from slowapi import Limiter
        from slowapi.util import get_remote_addr as _slowapi_remote
    except ImportError:
        return None  # slowapi not installed — rate limiting disabled

    settings = get_settings()
    _limiter = Limiter(key_func=_slowapi_remote, default_limits=[settings.ratelimit_global])
    return _limiter


def setup_rate_limiting(app) -> None:
    """
    Wire slowapi's exception handler + state into the FastAPI app.

    Called from ``app.py`` during startup. Safe to call even if slowapi
    is not installed (no-op).
    """
    limiter = get_limiter()
    if limiter is None:
        return

    try:
        from slowapi import _rate_limit_exceeded_handler
        from slowapi.errors import RateLimitExceeded
    except ImportError:
        return

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# --------------------------------------------------------------------------- #
#  Input validation helpers
# --------------------------------------------------------------------------- #
def validate_search_query(q: str) -> str:
    """
    Validate a search query string.

    - Rejects empty queries.
    - Enforces ``SEARCH_MAX_QUERY_CHARS``.
    - Strips control characters (allows common whitespace).

    Raises ``ValueError`` (caught by the route → HTTP 400) on violation.
    """
    max_chars = get_settings().search_max_query_chars
    if not q or not q.strip():
        raise ValueError("query must not be empty")
    if len(q) > max_chars:
        raise ValueError(f"query exceeds max length of {max_chars} chars")
    # Strip C0 control chars except \t \n \r
    cleaned = "".join(c for c in q if c >= " " or c in "\t\n\r")
    return cleaned.strip()
