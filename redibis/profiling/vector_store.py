"""
redibis.profiling.vector_store — pluggable golden vector store (strategy pattern).
"""

from __future__ import annotations

import abc
import logging
import os
from dataclasses import dataclass, field
from typing import Iterable, Optional

from redibis.profiling.golden import GoldenColumn, ScoredMatch
from redibis.profiling.vectorize import FEATURE_DIM
from redibis.store.golden_store import GoldenStore
from redibis.store.storage_backend import StorageBackend

log = logging.getLogger(__name__)

_VECTOR_STORE: Optional["VectorStore"] = None


def score_from_distance(distance: float, metric: str = "cosine") -> float:
    """Normalize backend distance to similarity score 0–1 (higher = closer)."""
    if metric == "cosine":
        return max(0.0, min(1.0, 1.0 - distance))
    # L2 — squash with inverse
    return max(0.0, min(1.0, 1.0 / (1.0 + distance)))


class VectorStore(abc.ABC):
    """Backend-agnostic golden vector store."""

    name: str = "base"

    @abc.abstractmethod
    def upsert(self, col: GoldenColumn) -> None: ...

    def upsert_many(self, cols: Iterable[GoldenColumn]) -> None:
        for c in cols:
            self.upsert(c)

    @abc.abstractmethod
    def delete(self, table: str, column: str) -> bool: ...

    @abc.abstractmethod
    def search(
        self,
        embedding: list[float],
        k: int = 5,
        same_spec_only: bool = True,
        filters: dict | None = None,
    ) -> list[ScoredMatch]: ...

    def rerank(self, query_fp: dict, matches: list[ScoredMatch]) -> list[ScoredMatch]:
        return matches

    @abc.abstractmethod
    def load(self) -> int: ...

    @abc.abstractmethod
    def count(self) -> int: ...

    def health(self) -> dict:
        return {"backend": self.name, "count": self.count(), "ready": True}


@dataclass
class VectorConfig:
    backend: str = "inmemory"
    dim: int = FEATURE_DIM
    metric: str = "cosine"
    pg_dsn: str = ""

    @classmethod
    def from_env(cls, global_settings: dict | None = None) -> VectorConfig:
        gs = (global_settings or {}).get("vector") or {}
        return cls(
            backend=(gs.get("backend") or os.getenv("REDIBIS_VECTOR_BACKEND") or "inmemory").lower(),
            dim=int(gs.get("dim") or os.getenv("REDIBIS_VECTOR_DIM") or FEATURE_DIM),
            metric=(gs.get("metric") or os.getenv("REDIBIS_VECTOR_METRIC") or "cosine").lower(),
            pg_dsn=gs.get("pg_dsn") or os.getenv("PG_DSN") or "",
        )


def build_vector_store(
    cfg: VectorConfig,
    golden_store: GoldenStore,
) -> VectorStore:
    backend = (cfg.backend or "inmemory").lower()
    if backend == "inmemory":
        from redibis.profiling.backends.inmemory import InMemoryVectorStore
        return InMemoryVectorStore(golden_store, dim=cfg.dim, metric=cfg.metric)
    if backend == "pgvector":
        from redibis.profiling.backends.pgvector import PgVectorStore
        return PgVectorStore(cfg.pg_dsn, golden_store, dim=cfg.dim)
    raise ValueError(f"unknown vector backend {backend!r}")


def _resolve_golden_store(backend: Optional[StorageBackend] = None) -> GoldenStore:
    from redibis.store.storage_backend import get_backend
    if backend is None:
        backend = get_backend()
    contracts_bucket = os.getenv("CONTRACTS_BUCKET", "active-contracts")
    return GoldenStore.from_env(backend, contracts_bucket)


def get_vector_store(
    *,
    cfg: Optional[VectorConfig] = None,
    golden_store: Optional[GoldenStore] = None,
    global_settings: dict | None = None,
    force_rebuild: bool = False,
) -> VectorStore:
    """Process-level singleton; hydrates from golden path on first build."""
    global _VECTOR_STORE
    if _VECTOR_STORE is not None and not force_rebuild:
        return _VECTOR_STORE
    resolved_cfg = cfg or VectorConfig.from_env(global_settings)
    gs = golden_store or _resolve_golden_store()
    try:
        store = build_vector_store(resolved_cfg, gs)
        store.load()
        _VECTOR_STORE = store
        return store
    except Exception as exc:
        log.warning("vector backend %r failed (%s); falling back to inmemory", resolved_cfg.backend, exc)
        from redibis.profiling.backends.inmemory import InMemoryVectorStore
        fallback = InMemoryVectorStore(gs, dim=resolved_cfg.dim, metric=resolved_cfg.metric)
        fallback.load()
        _VECTOR_STORE = fallback
        return fallback


def reset_vector_store() -> None:
    """Clear singleton (for tests / settings reload)."""
    global _VECTOR_STORE
    _VECTOR_STORE = None
