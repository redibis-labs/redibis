"""Shared helpers for optional sentence-transformers tests."""

from __future__ import annotations

import pytest


def require_sentence_transformer(model_name: str = "all-MiniLM-L6-v2"):
    """Return an embedder or skip when the package/model is unavailable (e.g. offline)."""
    pytest.importorskip("sentence_transformers")
    from redibis.memory.embedding import SentenceTransformerEmbeddingProvider

    try:
        return SentenceTransformerEmbeddingProvider(model_name)
    except Exception as exc:
        pytest.skip(f"sentence-transformers model unavailable: {exc}")
