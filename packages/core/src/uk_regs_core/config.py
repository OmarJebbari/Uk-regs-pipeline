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
    embedding_model: str = "text-embedding-3-small"

    # ---- Pipeline behaviour ----
    confidence_threshold: float = Field(
        default=0.85,
        description="Below this, route to human review",
    )
    reconciliation_top_k: int = 5

    # ---- API auth ----
    api_key: str | None = Field(
        default=None,
        description="If set, mutating routes require X-API-Key header. "
        "Unset = open (dev mode).",
    )

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
