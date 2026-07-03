"""
Embeddings client (OpenAI text-embedding-3-small by default).

Abstracted behind an interface so it can be swapped for a local model
(e.g. BGE/E5 via sentence-transformers or a vLLM endpoint) without touching
callers.
"""

from __future__ import annotations

import abc
from typing import Sequence

from uk_regs_core import Settings, get_settings


class Embedder(abc.ABC):
    @abc.abstractmethod
    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_text(self, text: str) -> list[float]:
        return self.embed_texts([text])[0]

    @property
    @abc.abstractmethod
    def dimension(self) -> int: ...


class OpenAIEmbedder(Embedder):
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        from openai import OpenAI

        if not self._settings.openai_api_key:
            from uk_regs_core import ConfigError

            raise ConfigError("OPENAI_API_KEY is required for OpenAIEmbedder")
        self._client = OpenAI(api_key=self._settings.openai_api_key)
        self._model = self._settings.embedding_model

    @property
    def dimension(self) -> int:
        return 1536  # text-embedding-3-small

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        resp = self._client.embeddings.create(input=list(texts), model=self._model)
        return [d.embedding for d in resp.data]


def get_embedder(settings: Settings | None = None) -> Embedder:
    return OpenAIEmbedder(settings)