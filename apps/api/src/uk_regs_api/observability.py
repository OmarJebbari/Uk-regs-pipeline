"""
Observability stack — structured logging, OpenTelemetry tracing, Langfuse.

All three are **opt-in** and degrade gracefully:
  - structlog always active (console in dev, JSON in prod)
  - OTel no-op when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is unset
  - Langfuse no-op when ``LANGFUSE_PUBLIC_KEY`` is unset

Importing this module configures everything; the FastAPI app calls
``setup_observability(app)`` on startup.
"""

from __future__ import annotations

import logging
import sys
import time
import uuid
from typing import TYPE_CHECKING

from uk_regs_core import get_settings

if TYPE_CHECKING:
    from fastapi import FastAPI

# --------------------------------------------------------------------------- #
#  Structured logging (structlog)
# --------------------------------------------------------------------------- #
def setup_logging() -> None:
    """Configure structlog + stdlib logging bridge."""
    import structlog

    settings = get_settings()
    is_json = settings.log_format == "json"

    # Shared processors for both structlog and the stdlib bridge
    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, settings.log_level)
        ),
        cache_logger_on_first_use=True,
    )

    # Bridge: route stdlib logging (uvicorn, sqlalchemy) through structlog
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            (
                structlog.processors.JSONRenderer()
                if is_json
                else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
            ),
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, settings.log_level))

    # Tame uvicorn's noisy internal loggers
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True


def get_logger(name: str = "uk-regs"):
    """Return a configured structlog logger."""
    import structlog

    return structlog.get_logger(name)


# --------------------------------------------------------------------------- #
#  Request logging middleware (request-id, latency, route)
# --------------------------------------------------------------------------- #
async def request_logging_middleware(request, call_next):  # type: ignore[no-untyped-def]
    """Attach request-id, log method+path+latency+status."""
    import structlog

    logger = structlog.get_logger("uk-regs.http")
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())

    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        request_id=request_id,
        method=request.method,
        path=request.url.path,
    )

    start = time.perf_counter()
    response = await call_next(request)
    latency_ms = round((time.perf_counter() - start) * 1000, 2)

    structlog.contextvars.bind_contextvars(
        status_code=response.status_code,
        latency_ms=latency_ms,
    )
    logger.info("request")
    response.headers["x-request-id"] = request_id
    return response


# --------------------------------------------------------------------------- #
#  OpenTelemetry tracing
# --------------------------------------------------------------------------- #
def setup_tracing(app: FastAPI) -> None:
    """Auto-instrument FastAPI + DB calls with OTel. No-op if endpoint unset."""
    settings = get_settings()
    if not settings.otel_exporter_otlp_endpoint:
        return  # tracing disabled in this environment

    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
        OTLPSpanExporter,
    )
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create(
        {"service.name": settings.otel_service_name}
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint)
        )
    )
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app)


# --------------------------------------------------------------------------- #
#  Langfuse — LLM observability (no-op unless keys present)
# --------------------------------------------------------------------------- #
def get_langfuse():
    """Return a Langfuse client or None when unconfigured."""
    settings = get_settings()
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        return None
    try:
        from langfuse import Langfuse

        return Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
        )
    except Exception:
        return None


def trace_llm_call(operation: str, model: str, **kwargs):  # type: ignore[no-untyped-def]
    """Context manager that wraps an LLM call in a Langfuse generation trace."""
    from contextlib import contextmanager

    langfuse = get_langfuse()

    @contextmanager
    def _null():
        yield None

    if langfuse is None:
        return _null()

    @contextmanager
    def _wrap():
        trace = langfuse.trace(name=operation)
        generation = trace.generation(
            name=operation,
            model=model,
            metadata=kwargs,
        )
        try:
            yield generation
        finally:
            generation.end()

    return _wrap()


# --------------------------------------------------------------------------- #
#  Entry point — wire everything
# --------------------------------------------------------------------------- #
def setup_observability(app: FastAPI) -> None:
    """Configure logging, tracing, and request middleware on the FastAPI app."""
    setup_logging()
    app.middleware("http")(request_logging_middleware)
    setup_tracing(app)