"""
redibis.profiling.inference — search golden index and return governance suggestions.
"""

from __future__ import annotations

from typing import Optional

from redibis.profiling.fingerprint import ColumnFingerprint
from redibis.profiling.golden import ScoredMatch
from redibis.profiling.vector_store import VectorStore, get_vector_store
from redibis.profiling.vectorize import fingerprint_to_vector


def infer_column_governance(
    fp: ColumnFingerprint,
    *,
    k: int = 5,
    min_score: float = 0.0,
    vector_store: Optional[VectorStore] = None,
) -> list[ScoredMatch]:
    """Search → rerank → threshold filter."""
    store = vector_store or get_vector_store()
    embedding = fingerprint_to_vector(fp)
    matches = store.search(embedding, k=k, same_spec_only=True)
    matches = store.rerank(fp.to_dict(), matches)
    return [m for m in matches if m.score >= min_score]
