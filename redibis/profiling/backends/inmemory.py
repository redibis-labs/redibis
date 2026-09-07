"""
redibis.profiling.backends.inmemory — FAISS in-process golden vector index (default backend).

``faiss-cpu`` is a core dependency; numpy scan is retained only when FAISS is
unavailable (e.g. broken install).
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional

import numpy as np

from redibis.profiling.golden import GoldenColumn, ScoredMatch
from redibis.profiling.vector_store import VectorStore, score_from_distance
from redibis.profiling.vectorize import FEATURE_DIM
from redibis.store.golden_store import GoldenStore

log = logging.getLogger(__name__)

_FAISS_AVAILABLE = False
try:
    import faiss  # type: ignore[import-untyped]
    _FAISS_AVAILABLE = True
except ImportError:
    faiss = None  # type: ignore[assignment]
    log.warning(
        "faiss-cpu is not installed — in-memory golden search falls back to numpy. "
        "Install redibis with default dependencies (pip install redibis) for FAISS."
    )


class InMemoryVectorStore(VectorStore):
    """FAISS flat index in process; metadata in a dict; durable via GoldenStore."""

    name = "inmemory"

    def __init__(
        self,
        golden_store: GoldenStore,
        dim: int = FEATURE_DIM,
        metric: str = "cosine",
    ):
        self.golden_store = golden_store
        self.dim = dim
        self.metric = metric
        self._cols: dict[str, GoldenColumn] = {}
        self._index = None
        self._id_order: list[str] = []
        self._ready = False

    def _build_faiss_index(self, vectors: np.ndarray) -> object:
        if self.metric == "cosine":
            faiss.normalize_L2(vectors)
            index = faiss.IndexFlatIP(self.dim)
        else:
            index = faiss.IndexFlatL2(self.dim)
        index.add(vectors)
        return index

    def _rebuild_index(self) -> None:
        if _FAISS_AVAILABLE and self._id_order:
            mat = np.array(
                [self._cols[k].embedding for k in self._id_order],
                dtype=np.float32,
            )
            self._index = self._build_faiss_index(mat)
        else:
            self._index = None

    def _stage_col(self, col: GoldenColumn) -> None:
        key = col.key()
        self._cols[key] = col
        if key not in self._id_order:
            self._id_order.append(key)
        else:
            self._id_order = [k for k in self._id_order if k != key] + [key]

    def load(self) -> int:
        cols = self.golden_store.list_all()
        self._cols.clear()
        self._id_order.clear()
        self._index = None
        for col in cols:
            if len(col.embedding) != self.dim:
                log.warning(
                    "skip golden %s: embedding dim %d != %d",
                    col.key(), len(col.embedding), self.dim,
                )
                continue
            self._stage_col(col)
        self._rebuild_index()
        self._ready = True
        return len(self._cols)

    def upsert(self, col: GoldenColumn) -> None:
        self.upsert_many([col])

    def upsert_many(self, cols: Iterable[GoldenColumn]) -> None:
        batch = list(cols)
        if not batch:
            return
        for col in batch:
            self.golden_store.write(col)
            self._stage_col(col)
        self._rebuild_index()

    def delete(self, table: str, column: str) -> bool:
        key = f"{table}::{column}"
        self.golden_store.delete(table, column)
        if key not in self._cols:
            return False
        del self._cols[key]
        self._id_order = [k for k in self._id_order if k != key]
        self._rebuild_index()
        return True

    def _numpy_search(
        self,
        embedding: list[float],
        k: int,
        same_spec_only: bool,
        spec: Optional[str],
    ) -> list[ScoredMatch]:
        q = np.array(embedding, dtype=np.float32)
        q_norm = np.linalg.norm(q)
        if q_norm > 0:
            q = q / q_norm
        matches: list[ScoredMatch] = []
        for key in self._id_order:
            col = self._cols[key]
            if same_spec_only and spec and col.feature_spec_version != spec:
                continue
            v = np.array(col.embedding, dtype=np.float32)
            v_norm = np.linalg.norm(v)
            if v_norm <= 0:
                continue
            v = v / v_norm
            if self.metric == "cosine":
                dist = 1.0 - float(np.dot(q, v))
            else:
                dist = float(np.linalg.norm(q - v))
            matches.append(ScoredMatch(col, score_from_distance(dist, self.metric), dist))
        matches.sort(key=lambda m: m.distance)
        return matches[:k]

    def search(
        self,
        embedding: list[float],
        k: int = 5,
        same_spec_only: bool = True,
        filters: dict | None = None,
    ) -> list[ScoredMatch]:
        if not self._cols:
            return []
        from redibis.profiling.fingerprint import FEATURE_SPEC_VERSION
        spec = FEATURE_SPEC_VERSION if same_spec_only else None

        if _FAISS_AVAILABLE and self._index is not None:
            q = np.array([embedding], dtype=np.float32)
            if self.metric == "cosine":
                faiss.normalize_L2(q)
            distances, indices = self._index.search(q, min(k * 3, len(self._id_order)))
            matches: list[ScoredMatch] = []
            for dist, idx in zip(distances[0], indices[0]):
                if idx < 0 or idx >= len(self._id_order):
                    continue
                col = self._cols[self._id_order[idx]]
                if same_spec_only and col.feature_spec_version != spec:
                    continue
                if filters:
                    if filters.get("classification") and col.classification != filters["classification"]:
                        continue
                raw_dist = 1.0 - float(dist) if self.metric == "cosine" else float(dist)
                matches.append(ScoredMatch(col, score_from_distance(raw_dist, self.metric), raw_dist))
                if len(matches) >= k:
                    break
            return matches

        return self._numpy_search(embedding, k, same_spec_only, spec)

    def count(self) -> int:
        return len(self._cols)

    def health(self) -> dict:
        return {
            "backend": self.name,
            "count": self.count(),
            "ready": self._ready,
            "faiss": _FAISS_AVAILABLE,
            "metric": self.metric,
            "dim": self.dim,
        }
