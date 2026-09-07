"""
redibis.memory.store — vector memory store (pgvector + in-memory test double).
"""

from __future__ import annotations

import json
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from redibis.config import ConfigError, MemoryConfig
from redibis.memory.dsn import resolve_dsn_ref
from redibis.memory.embedding import EmbeddingProvider, get_embedding_provider


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_fingerprint_key(
    name_normalized: str,
    logical_type: str,
    format_signature: str,
) -> str:
    return f"{name_normalized}|{logical_type}|{format_signature}"


@dataclass
class MemoryUpsertResult:
    canonical_key: str
    occurrence_count: int
    created: bool


@dataclass
class MemorySearchResult:
    canonical_key: str
    similarity: float
    fingerprint: dict
    decisions: list[dict] = field(default_factory=list)
    occurrence_count: int = 1


@dataclass
class MemoryListEntry:
    """One row in the column memory store (format-signature keyed)."""

    canonical_key: str
    fingerprint: dict
    decisions: list[dict] = field(default_factory=list)
    occurrence_count: int = 1
    domain: str = ""
    logical_type: str = ""
    format_signature: str = ""
    entity_type: Optional[str] = None
    updated_at: str = ""


class MemoryStore(ABC):
    @abstractmethod
    def ensure_schema(self) -> None:
        ...

    @abstractmethod
    def upsert(
        self,
        *,
        canonical_key: str,
        fingerprint: dict,
        embedding: list[float],
        decision: dict,
        domain: str = "",
        logical_type: str = "",
        format_signature: str = "",
        entity_type: Optional[str] = None,
        column_card: str = "",
    ) -> MemoryUpsertResult:
        ...

    @abstractmethod
    def search(
        self,
        embedding: list[float],
        *,
        top_k: int = 5,
        min_similarity: float = 0.0,
        logical_type: Optional[str] = None,
        format_signature: Optional[str] = None,
        domain: Optional[str] = None,
    ) -> list[MemorySearchResult]:
        ...

    @abstractmethod
    def list_entries(self, *, limit: int = 500) -> list[MemoryListEntry]:
        ...


class InMemoryMemoryStore(MemoryStore):
    """Test/dev double with cosine similarity (no Postgres required)."""

    def __init__(self) -> None:
        self._rows: dict[str, dict[str, Any]] = {}

    def ensure_schema(self) -> None:
        return

    def upsert(
        self,
        *,
        canonical_key: str,
        fingerprint: dict,
        embedding: list[float],
        decision: dict,
        domain: str = "",
        logical_type: str = "",
        format_signature: str = "",
        entity_type: Optional[str] = None,
        column_card: str = "",
    ) -> MemoryUpsertResult:
        row = self._rows.get(canonical_key)
        if row is None:
            self._rows[canonical_key] = {
                "canonical_key": canonical_key,
                "fingerprint": fingerprint,
                "embedding": embedding,
                "decisions": [decision],
                "occurrence_count": 1,
                "domain": domain,
                "logical_type": logical_type,
                "format_signature": format_signature,
                "entity_type": entity_type,
                "column_card": column_card,
                "updated_at": _utc_now_iso(),
            }
            return MemoryUpsertResult(canonical_key, 1, created=True)

        row["occurrence_count"] = int(row.get("occurrence_count", 1)) + 1
        row["decisions"].append(decision)
        row["fingerprint"] = fingerprint
        row["embedding"] = embedding
        row["column_card"] = column_card
        row["updated_at"] = _utc_now_iso()
        return MemoryUpsertResult(
            canonical_key, row["occurrence_count"], created=False,
        )

    def search(
        self,
        embedding: list[float],
        *,
        top_k: int = 5,
        min_similarity: float = 0.0,
        logical_type: Optional[str] = None,
        format_signature: Optional[str] = None,
        domain: Optional[str] = None,
    ) -> list[MemorySearchResult]:
        hits: list[MemorySearchResult] = []
        for row in self._rows.values():
            if logical_type and row.get("logical_type") != logical_type:
                continue
            if format_signature and row.get("format_signature") != format_signature:
                continue
            sim = _cosine(embedding, row.get("embedding") or [])
            if sim < min_similarity:
                continue
            hits.append(MemorySearchResult(
                canonical_key=row["canonical_key"],
                similarity=sim,
                fingerprint=row.get("fingerprint") or {},
                decisions=list(row.get("decisions") or []),
                occurrence_count=int(row.get("occurrence_count") or 1),
            ))
        hits.sort(key=lambda h: h.similarity, reverse=True)
        return hits[:top_k]

    def list_entries(self, *, limit: int = 500) -> list[MemoryListEntry]:
        rows = sorted(
            self._rows.values(),
            key=lambda r: r.get("updated_at") or "",
            reverse=True,
        )
        return [
            MemoryListEntry(
                canonical_key=row["canonical_key"],
                fingerprint=dict(row.get("fingerprint") or {}),
                decisions=list(row.get("decisions") or []),
                occurrence_count=int(row.get("occurrence_count") or 1),
                domain=str(row.get("domain") or ""),
                logical_type=str(row.get("logical_type") or ""),
                format_signature=str(row.get("format_signature") or ""),
                entity_type=row.get("entity_type"),
                updated_at=str(row.get("updated_at") or ""),
            )
            for row in rows[:limit]
        ]


class PgVectorMemoryStore(MemoryStore):
    """Postgres + pgvector backend with HNSW index and canonical upsert."""

    def __init__(self, dsn: str, *, embedding: EmbeddingProvider) -> None:
        self._dsn = dsn
        self._embedding = embedding
        self._conn = None
        self._schema_ready = False

    def _connect(self):
        if self._conn is None:
            try:
                import psycopg2  # type: ignore
            except ImportError as exc:
                raise ConfigError(
                    "psycopg2 is required for pgvector memory; "
                    "install with: pip install redibis[memory]"
                ) from exc
            self._conn = psycopg2.connect(self._dsn)
            self._conn.autocommit = True
        return self._conn

    def ensure_schema(self) -> None:
        if self._schema_ready:
            return
        dim = self._embedding.dimension
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS column_memory (
                    id BIGSERIAL PRIMARY KEY,
                    canonical_key TEXT UNIQUE NOT NULL,
                    occurrence_count INT NOT NULL DEFAULT 1,
                    name_normalized TEXT,
                    logical_type TEXT,
                    format_signature TEXT,
                    domain TEXT,
                    entity_type TEXT,
                    column_card TEXT,
                    embedding vector({dim}),
                    fingerprint JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                    decisions JSONB NOT NULL DEFAULT '[]'::jsonb,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS column_memory_hnsw_idx
                ON column_memory USING hnsw (embedding vector_cosine_ops)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS column_memory_type_fmt_idx
                ON column_memory (logical_type, format_signature)
                """
            )
        self._schema_ready = True

    def upsert(
        self,
        *,
        canonical_key: str,
        fingerprint: dict,
        embedding: list[float],
        decision: dict,
        domain: str = "",
        logical_type: str = "",
        format_signature: str = "",
        entity_type: Optional[str] = None,
        column_card: str = "",
    ) -> MemoryUpsertResult:
        self.ensure_schema()
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT occurrence_count, decisions FROM column_memory WHERE canonical_key = %s",
                (canonical_key,),
            )
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    """
                    INSERT INTO column_memory (
                        canonical_key, occurrence_count, name_normalized, logical_type,
                        format_signature, domain, entity_type, column_card,
                        embedding, fingerprint, decisions, updated_at
                    ) VALUES (%s, 1, %s, %s, %s, %s, %s, %s, %s::vector, %s::jsonb, %s::jsonb, NOW())
                    """,
                    (
                        canonical_key,
                        fingerprint.get("name_normalized", ""),
                        logical_type,
                        format_signature,
                        domain,
                        entity_type,
                        column_card,
                        _vector_literal(embedding),
                        json.dumps(fingerprint),
                        json.dumps([decision]),
                    ),
                )
                return MemoryUpsertResult(canonical_key, 1, created=True)

            count = int(row[0]) + 1
            decisions = list(row[1] or [])
            decisions.append(decision)
            cur.execute(
                """
                UPDATE column_memory
                SET occurrence_count = %s,
                    fingerprint = %s::jsonb,
                    decisions = %s::jsonb,
                    embedding = %s::vector,
                    column_card = %s,
                    domain = %s,
                    entity_type = %s,
                    updated_at = NOW()
                WHERE canonical_key = %s
                """,
                (
                    count,
                    json.dumps(fingerprint),
                    json.dumps(decisions),
                    _vector_literal(embedding),
                    column_card,
                    domain,
                    entity_type,
                    canonical_key,
                ),
            )
            return MemoryUpsertResult(canonical_key, count, created=False)

    def search(
        self,
        embedding: list[float],
        *,
        top_k: int = 5,
        min_similarity: float = 0.0,
        logical_type: Optional[str] = None,
        format_signature: Optional[str] = None,
        domain: Optional[str] = None,
    ) -> list[MemorySearchResult]:
        self.ensure_schema()
        conn = self._connect()
        vec = _vector_literal(embedding)
        where = ["1 - (embedding <=> %s::vector) >= %s"]
        params: list[Any] = [vec, min_similarity]
        if logical_type:
            where.append("logical_type = %s")
            params.append(logical_type)
        if format_signature:
            where.append("format_signature = %s")
            params.append(format_signature)
        order = "embedding <=> %s::vector"
        order_params: list[Any] = [vec]
        if domain:
            order = f"CASE WHEN domain = %s THEN 0 ELSE 1 END, {order}"
            order_params = [domain, vec]
        sql = f"""
            SELECT canonical_key,
                   1 - (embedding <=> %s::vector) AS similarity,
                   fingerprint, decisions, occurrence_count
            FROM column_memory
            WHERE {' AND '.join(where)}
            ORDER BY {order}
            LIMIT %s
        """
        exec_params = [vec] + params + order_params + [top_k]
        with conn.cursor() as cur:
            cur.execute(sql, exec_params)
            rows = cur.fetchall()
        return [
            MemorySearchResult(
                canonical_key=r[0],
                similarity=float(r[1]),
                fingerprint=r[2] or {},
                decisions=list(r[3] or []),
                occurrence_count=int(r[4] or 1),
            )
            for r in rows
        ]

    def list_entries(self, *, limit: int = 500) -> list[MemoryListEntry]:
        self.ensure_schema()
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT canonical_key, fingerprint, decisions, occurrence_count,
                       domain, logical_type, format_signature, entity_type, updated_at
                FROM column_memory
                ORDER BY updated_at DESC
                LIMIT %s
                """,
                (max(1, int(limit)),),
            )
            rows = cur.fetchall()
        return [
            MemoryListEntry(
                canonical_key=r[0],
                fingerprint=dict(r[1] or {}),
                decisions=list(r[2] or []),
                occurrence_count=int(r[3] or 1),
                domain=str(r[4] or ""),
                logical_type=str(r[5] or ""),
                format_signature=str(r[6] or ""),
                entity_type=r[7],
                updated_at=str(r[8] or ""),
            )
            for r in rows
        ]


def _vector_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.8f}" for x in vec) + "]"


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


MEMORY_STORE_REGISTRY: dict[str, type] = {
    "pgvector": PgVectorMemoryStore,
    "memory": InMemoryMemoryStore,
}


def get_memory_store(
    config: MemoryConfig,
    *,
    embedding: Optional[EmbeddingProvider] = None,
    dsn: Optional[str] = None,
) -> Optional[MemoryStore]:
    if not config.enabled:
        return None
    store_key = (config.store or "pgvector").lower()
    if store_key == "memory":
        return InMemoryMemoryStore()
    embedder = embedding or get_embedding_provider(config)
    if store_key == "pgvector":
        ref = dsn or resolve_dsn_ref(config.dsn_ref)
        return PgVectorMemoryStore(ref, embedding=embedder)
    raise ConfigError(
        f"unknown memory store {config.store!r}; choices: pgvector, memory"
    )
