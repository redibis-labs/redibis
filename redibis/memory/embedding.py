"""
redibis.memory.embedding — on-prem embedding providers (lazy heavy imports).
"""

from __future__ import annotations

import hashlib
import math
from abc import ABC, abstractmethod
from typing import Optional

from redibis.config import ConfigError, MemoryConfig

_DEFAULT_DIM = 384


class EmbeddingProvider(ABC):
    @abstractmethod
    def embed(self, text: str) -> list[float]:
        """Return a dense embedding vector for ``text``."""

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple texts; override for model-native batching."""
        return [self.embed(text) for text in texts]

    @property
    @abstractmethod
    def dimension(self) -> int:
        ...


class HashEmbeddingProvider(EmbeddingProvider):
    """Deterministic hash embedder for unit tests only — not semantically meaningful."""

    def __init__(self, *, dimension: int = _DEFAULT_DIM) -> None:
        self._dimension = dimension

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed(self, text: str) -> list[float]:
        seed = hashlib.sha256((text or "").encode("utf-8")).digest()
        out: list[float] = []
        counter = 0
        while len(out) < self._dimension:
            block = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
            counter += 1
            for i in range(0, len(block), 4):
                if len(out) >= self._dimension:
                    break
                chunk = int.from_bytes(block[i:i + 4], "big", signed=False)
                out.append((chunk % 10_000) / 10_000.0 - 0.5)
        norm = math.sqrt(sum(x * x for x in out)) or 1.0
        return [x / norm for x in out]


class SentenceTransformerEmbeddingProvider(EmbeddingProvider):
    def __init__(self, model_name: str) -> None:
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
        except ImportError as exc:
            raise ConfigError(
                "sentence-transformers is required for local embeddings; "
                "install with: pip install redibis[memory]"
            ) from exc
        self._model = SentenceTransformer(model_name)
        dim_fn = getattr(self._model, "get_embedding_dimension", None)
        if callable(dim_fn):
            dim = dim_fn()
        else:
            dim = self._model.get_sentence_embedding_dimension()
        self._dimension = int(dim) if dim else _DEFAULT_DIM

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed(self, text: str) -> list[float]:
        vec = self._model.encode(text or "", normalize_embeddings=True)
        return [float(x) for x in vec]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vecs = self._model.encode(texts, normalize_embeddings=True)
        return [[float(x) for x in row] for row in vecs]


EMBEDDING_REGISTRY: dict[str, type[EmbeddingProvider]] = {
    "local": HashEmbeddingProvider,
    "hash": HashEmbeddingProvider,
    "sentence_transformers": SentenceTransformerEmbeddingProvider,
}


def get_embedding_provider(config: MemoryConfig) -> EmbeddingProvider:
    provider = (config.embedding_provider or "sentence_transformers").lower()
    if provider in ("local", "hash"):
        return HashEmbeddingProvider()
    if provider in ("sentence_transformers", "st"):
        model = config.embedding_model or "all-MiniLM-L6-v2"
        return SentenceTransformerEmbeddingProvider(model)
    cls = EMBEDDING_REGISTRY.get(provider)
    if cls is None:
        raise ConfigError(
            f"unknown embedding provider {config.embedding_provider!r}; "
            f"choices: {sorted(EMBEDDING_REGISTRY)}"
        )
    return cls()
