"""
Typed application configuration (12-factor).
Reads from environment / .env via pydantic-settings.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- General ----
    app_name: str = "uk-regs-pipeline"
    env: Literal["dev", "staging", "prod"] = "dev"
    debug: bool = False
    # Interactive docs (Swagger/ReDoc/OpenAPI) are served in dev by default.
    # In staging/prod set DOCS_ENABLED=true to expose them explicitly.
    docs_enabled: bool = False

    # ---- Postgres + pgvector ----
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "ukregs"
    postgres_user: str = "ukregs"
    postgres_password: str = "ukregs"

    @property
    def postgres_dsn(self) -> str:
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    # ---- Neo4j ----
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "password"

    # ---- Object storage (MinIO / S3) ----
    s3_endpoint: str = "http://localhost:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_bucket: str = "uk-regs"
    s3_region: str = "us-east-1"

    # ---- LLM ----
    llm_provider: Literal["openai", "anthropic", "bedrock", "vllm"] = "openai"
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o"
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-3-5-sonnet-20241022"
    llm_temperature: float = 0.0
    llm_timeout_s: float = 60.0

    # ---- Embeddings ----
    # Provider selector: "openai" (default), "cohere", or "mock" (deterministic
    # hash-based vectors for tests / offline dev — no network calls).
    embedding_provider: Literal["openai", "cohere", "mock"] = "openai"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimension: int = 1536
    # Cohere
    cohere_api_key: str | None = None
    cohere_model: str = "embed-english-v3.0"
    cohere_input_type: str = "search_document"

    # ---- Pipeline behaviour ----
    confidence_threshold: float = Field(
        default=0.85,
        description="Below this, route to human review",
    )
    reconciliation_top_k: int = 5

    # ---- Hybrid search (Phase 5-2) ----
    # Default search mode for the API / MCP server. 'hybrid' fuses BM25 + vector
    # via Reciprocal Rank Fusion; 'semantic' / 'keyword' use one leg only.
    search_mode: Literal["hybrid", "semantic", "keyword"] = "hybrid"
    # RRF smoothing constant. Higher = flatter rank distribution. 60 is the
    # literature default (Cormack et al., 2009).
    search_rrf_k: int = Field(default=60, ge=1)
    # Per-leg candidate fetch depth. Each leg retrieves this many candidates
    # before RRF re-ranks and truncates to `top_k`. Higher = better recall,
    # more DB work.
    search_candidate_k: int = Field(default=50, ge=1, le=500)
    # Minimum fused score to return a hit (filters noise). 0.0 returns all;
    # 1/(rrf_k+1) is roughly the score of a result ranked #1 in one leg only.
    search_min_score: float = Field(default=0.0, ge=0.0, le=1.0)

    # ---- API auth ----
    api_key: str | None = Field(
        default=None,
        description="If set, mutating routes require X-API-Key header. Unset = open (dev mode).",
    )

    # ---- Rate limiting (slowapi) ----
    # Format: "<count> per <unit>" e.g. "60/minute". Set to empty to disable.
    # Only set TRUST_PROXY=true when the app runs behind a trusted reverse
    # proxy / load balancer that overwrites X-Forwarded-For. Otherwise a
    # spoofable client IP would let attackers bypass per-IP limits.
    trust_proxy: bool = Field(
        default=False,
        description="Honour X-Forwarded-For for rate-limit keying. Only enable "
        "behind a trusted proxy that overwrites the header.",
    )
    ratelimit_global: str = Field(
        default="200/minute",
        description="Global per-IP rate limit applied to all API routes.",
    )
    ratelimit_search: str = Field(
        default="60/minute",
        description="Per-IP limit for expensive /search queries (vector + BM25).",
    )
    ratelimit_mutate: str = Field(
        default="20/minute",
        description="Per-IP limit for mutating routes (review, ingest, export).",
    )
    # Maximum query length (chars) accepted by /search to prevent abuse.
    search_max_query_chars: int = Field(default=2000, ge=10, le=10_000)

    # ---- Logging (structlog) ----
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "console"] = "console"

    # ---- OpenTelemetry ----
    otel_exporter_otlp_endpoint: str | None = Field(
        default=None,
        description="OTLP collector endpoint, e.g. http://localhost:4317. "
        "Unset = tracing disabled.",
    )
    otel_service_name: str = "uk-regs-api"

    # ---- Langfuse (LLM observability) ----
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_host: str = "https://cloud.langfuse.com"


@lru_cache
def get_settings() -> Settings:
    return Settings()
