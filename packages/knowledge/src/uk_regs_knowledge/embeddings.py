"""
Embeddings provider abstraction.

Three backends are supported, selected via ``EMBEDDING_PROVIDER``:

* ``openai`` — OpenAI ``text-embedding-3-small`` (default, 1536-dim).
* ``cohere`` — Cohere ``embed-english-v3.0`` (1024-dim).
* ``mock``   — Deterministic hash-based vectors (no network). Used in tests and
  offline development.

All backends implement the :class:`Embedder` interface, so callers (the ingest
pipeline, search service, evals) are fully decoupled from the concrete provider.

Adding a new provider (e.g. sentence-transformers / vLLM) only requires:

1. Add a new ``Literal`` value to ``embedding_provider`` in ``Settings``.
2. Subclass ``Embedder`` and implement ``embed_texts`` / ``dimension``.
3. Add a branch to :func:`get_embedder`.
"""

from __future__ import annotations

import abc
import hashlib
import struct
from collections.abc import Sequence

from uk_regs_core import Settings, get_settings


class Embedder(abc.ABC):
    """Abstract embeddings interface."""

    @abc.abstractmethod
    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed a batch of texts and return one vector per input."""
        ...

    def embed_text(self, text: str) -> list[float]:
        """Embed a single text (convenience wrapper)."""
        return self.embed_texts([text])[0]

    @property
    @abc.abstractmethod
    def dimension(self) -> int:
        """Vector dimensionality produced by this embedder."""
        ...


# ---------------------------------------------------------------------------
# Mock embedder — deterministic, no network, for tests / offline dev.
# ---------------------------------------------------------------------------


class MockEmbedder(Embedder):
    """
    Deterministic hash-based embedder.

    Each input text is SHA-256-hashed, then the digest is unpacked into
    ``dimension`` floats in ``[-1, 1]`` and L2-normalised. This means:

    * Same text → same vector (reproducible tests, stable pgvector indexes).
    * Different texts → very likely different vectors (orthogonal-ish).
    * No external dependencies or API keys.
    """

    def __init__(self, dimension: int = 1536) -> None:
        self._dimension = dimension

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        results: list[list[float]] = []
        for text in texts:
            # Generate enough bytes: 4 bytes per float.
            needed = self._dimension * 4
            digest = b""
            counter = 0
            while len(digest) < needed:
                digest += hashlib.sha256(f"{text}:{counter}".encode()).digest()
                counter += 1

            floats = list(struct.unpack(f"<{self._dimension}f", digest[:needed]))
            # Normalise to [-1, 1] then L2-normalise.
            vec = [(f / 2.0 - 0.5) * 2.0 for f in floats]
            norm = sum(v * v for v in vec) ** 0.5
            if norm > 0:
                vec = [v / norm for v in vec]
            results.append(vec)
        return results


# ---------------------------------------------------------------------------
# OpenAI embedder
# ---------------------------------------------------------------------------


class OpenAIEmbedder(Embedder):
    """OpenAI ``text-embedding-3-small`` (1536-dim by default)."""

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
        return self._settings.embedding_dimension

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        resp = self._client.embeddings.create(input=list(texts), model=self._model)
        return [d.embedding for d in resp.data]


# ---------------------------------------------------------------------------
# Cohere embedder
# ---------------------------------------------------------------------------


class CohereEmbedder(Embedder):
    """Cohere ``embed-english-v3.0`` (1024-dim by default)."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        try:
            import cohere
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "cohere package is required for CohereEmbedder. Install with: uv pip install cohere"
            ) from exc

        if not self._settings.cohere_api_key:
            from uk_regs_core import ConfigError

            raise ConfigError("COHERE_API_KEY is required for CohereEmbedder")
        self._client = cohere.Client(api_key=self._settings.cohere_api_key)
        self._model = self._settings.cohere_model
        self._input_type = self._settings.cohere_input_type

    @property
    def dimension(self) -> int:
        # embed-english-v3.0 = 1024; embed-multilingual-v3.0 = 1024.
        return self._settings.embedding_dimension

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        resp = self._client.embed(
            texts=list(texts),
            model=self._model,
            input_type=self._input_type,
        )
        return list(resp.embeddings)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_embedder(settings: Settings | None = None) -> Embedder:
    """
    Return the configured :class:`Embedder` instance.

    Provider is selected via ``settings.embedding_provider``:

    * ``openai`` → :class:`OpenAIEmbedder`
    * ``cohere`` → :class:`CohereEmbedder`
    * ``mock``   → :class:`MockEmbedder`
    """
    settings = settings or get_settings()
    provider = settings.embedding_provider.lower()

    if provider == "mock":
        return MockEmbedder(dimension=settings.embedding_dimension)
    if provider == "cohere":
        return CohereEmbedder(settings)
    if provider == "openai":
        return OpenAIEmbedder(settings)

    raise ValueError(
        f"Unknown embedding_provider '{provider}'. Must be one of: openai, cohere, mock."
    )
