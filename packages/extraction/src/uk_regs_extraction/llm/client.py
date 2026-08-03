"""
Abstract LLM client with a thin OpenAI implementation.

Keeps the extraction layer provider-agnostic. Swap the implementation here
(Anthropic, Bedrock, vLLM) without touching the extraction logic.
"""

from __future__ import annotations

import abc
from typing import TypeVar

from pydantic import BaseModel
from tenacity import retry, stop_after_attempt, wait_exponential
from uk_regs_core import Settings, get_settings

T = TypeVar("T", bound=BaseModel)


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
        return self._client.chat.completions.create(
            model=self._model,
            response_model=response_model,
            temperature=temperature,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
        )

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    def describe_image(self, *, system: str, image_b64: str, mime: str = "image/png") -> str:
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
        return resp.choices[0].message.content or ""


def get_llm_client(settings: Settings | None = None) -> LLMClient:
    """Factory wired to the configured provider."""
    s = settings or get_settings()
    if s.llm_provider == "openai":
        return OpenAIClient(s)
    # Add AnthropicClient / BedrockClient here as needed.
    raise NotImplementedError(f"Provider '{s.llm_provider}' not implemented yet")
