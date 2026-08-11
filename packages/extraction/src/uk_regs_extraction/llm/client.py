"""
Abstract LLM client with a thin OpenAI implementation.

Keeps the extraction layer provider-agnostic. Swap the implementation here
(Anthropic, Bedrock, vLLM) without touching the extraction logic.

Every call is instrumented: latency, token usage, and estimated cost are
logged via structlog and (optionally) sent to Langfuse for dashboards.
"""

from __future__ import annotations

import abc
import logging
import time
from typing import TypeVar

from pydantic import BaseModel
from tenacity import retry, stop_after_attempt, wait_exponential
from uk_regs_core import Settings, get_settings

T = TypeVar("T", bound=BaseModel)

logger = logging.getLogger(__name__)

# --- Cost table (USD per 1K tokens, as of 2024-2025 public pricing) ---------
# Used for rough cost estimation in logs. Update when model pricing changes.
_COST_PER_1K = {
    # OpenAI — format: (input, output)
    "gpt-4o": (0.0025, 0.010),
    "gpt-4o-mini": (0.00015, 0.0006),
    "gpt-4-turbo": (0.010, 0.030),
    "gpt-4-vision-preview": (0.010, 0.030),
    "gpt-3.5-turbo": (0.0005, 0.0015),
    # Anthropic
    "claude-3-5-sonnet-20241022": (0.003, 0.015),
    "claude-3-opus-20240229": (0.015, 0.075),
}


def _estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Rough cost estimate in USD based on published per-1K-token pricing."""
    rates = _COST_PER_1K.get(model, (0.0, 0.0))
    return round(
        (prompt_tokens / 1000) * rates[0] + (completion_tokens / 1000) * rates[1],
        6,
    )


class LLMClient(abc.ABC):
    """Provider-agnostic structured-output LLM client."""

    @abc.abstractmethod
    def complete(
        self,
        *,
        system: str,
        user: str,
        response_model: type[T],
        temperature: float = 0.0,
        image_b64: str | None = None,
        image_mime: str = "image/png",
    ) -> T: ...

    @abc.abstractmethod
    def describe_image(self, *, system: str, image_b64: str, mime: str = "image/png") -> str:
        """Vision: natural-language description of a diagram."""


class OpenAIClient(LLMClient):
    """Default implementation. Uses instructor for structured outputs."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        # imported lazily so the package imports without openai installed
        import instructor
        from openai import OpenAI

        if not self._settings.openai_api_key:
            from uk_regs_core import ConfigError

            raise ConfigError("OPENAI_API_KEY is not set")

        self._raw = OpenAI(api_key=self._settings.openai_api_key)
        self._client = instructor.from_openai(self._raw)
        self._model = self._settings.openai_model

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    def complete(
        self,
        *,
        system: str,
        user: str,
        response_model: type[T],
        temperature: float = 0.0,
        image_b64: str | None = None,
        image_mime: str = "image/png",
    ) -> T:
        user_content: list[dict] = [{"type": "text", "text": user}]
        if image_b64:
            user_content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{image_mime};base64,{image_b64}"},
                }
            )
        t0 = time.perf_counter()
        # create_with_completion returns (structured_result, raw_completion)
        # so we can read token usage from the raw response object.
        result, completion = self._client.chat.completions.create_with_completion(
            model=self._model,
            response_model=response_model,
            temperature=temperature,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
        )
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        usage = getattr(completion, "usage", None)
        self._log_call(
            operation="complete",
            latency_ms=latency_ms,
            usage=usage,
            has_image=image_b64 is not None,
        )
        return result

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    def describe_image(self, *, system: str, image_b64: str, mime: str = "image/png") -> str:
        t0 = time.perf_counter()
        resp = self._raw.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{image_b64}"},
                        },
                    ],
                },
            ],
            temperature=0.0,
            max_tokens=600,
        )
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        self._log_call(
            operation="describe_image",
            latency_ms=latency_ms,
            usage=getattr(resp, "usage", None),
            has_image=True,
        )
        return resp.choices[0].message.content or ""

    def _log_call(
        self,
        *,
        operation: str,
        latency_ms: float,
        usage: object | None,
        has_image: bool,
    ) -> None:
        """Emit a structured log line with token usage + estimated cost."""
        prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
        completion_tokens = getattr(usage, "completion_tokens", 0) or 0
        total_tokens = getattr(usage, "total_tokens", 0) or (prompt_tokens + completion_tokens)
        cost_usd = _estimate_cost(self._model, prompt_tokens, completion_tokens)
        logger.info(
            "llm_call",
            extra={
                "llm_operation": operation,
                "llm_model": self._model,
                "llm_latency_ms": latency_ms,
                "llm_prompt_tokens": prompt_tokens,
                "llm_completion_tokens": completion_tokens,
                "llm_total_tokens": total_tokens,
                "llm_cost_usd": cost_usd,
                "llm_has_image": has_image,
            },
        )


def get_llm_client(settings: Settings | None = None) -> LLMClient:
    """Factory wired to the configured provider."""
    s = settings or get_settings()
    if s.llm_provider == "openai":
        return OpenAIClient(s)
    # Add AnthropicClient / BedrockClient here as needed.
    raise NotImplementedError(f"Provider '{s.llm_provider}' not implemented yet")
