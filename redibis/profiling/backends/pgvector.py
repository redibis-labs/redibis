"""
redibis.profiling.backends.pgvector — Postgres/pgvector golden column index.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from redibis.profiling.golden import GoldenColumn, ScoredMatch
from redibis.profiling.vector_store import VectorStore, score_from_distance
from redibis.profiling.vectorize import FEATURE_DIM
from redibis.store.golden_store import GoldenStore

log = logging.getLogger(__name__)

_PG_AVAILABLE = False
try:
    import psycopg2
    from psycopg2.extras import Json
    from pgvector.psycopg2 import register_vector
    _PG_AVAILABLE = True
except ImportError:
    psycopg2 = None  # type: ignore[assignment]
    Json = None  # type: ignore[assignment,misc]
    register_vector = None  # type: ignore[assignment]

_DDL = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS golden_columns (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    table_name text NOT NULL,
    column_name text NOT NULL,
    contract_uuid uuid,
    embedding vector({dim}) NOT NULL,
    fingerprint jsonb NOT NULL,
    classification text,
    entity_type text,
    tags text[],
    glossary text,
    synonyms text[],
    masking_policy jsonb,
    privacy jsonb,
    feature_spec_version text NOT NULL,
    approved_by text,
    approved_at timestamptz DEFAULT now(),
    UNIQUE (table_name, column_name)
);
CREATE INDEX IF NOT EXISTS golden_columns_emb_idx
    ON golden_columns USING hnsw (embedding vector_cosine_ops);
"""


class PgVectorStore(VectorStore):
    """psycopg + pgvector. GoldenStore remains the durable export mirror."""

    name = "pgvector"

    def __init__(self, dsn: str, golden_store: GoldenStore, dim: int = FEATURE_DIM):
        if not _PG_AVAILABLE:
            raise ImportError("pgvector backend requires psycopg2 and pgvector packages")
        if not dsn:
            raise ValueError("PG_DSN is required for pgvector backend")
        self.dsn = dsn
        self.golden_store = golden_store
        self.dim = dim
        self._conn = None
        self._count = 0

    def _connect(self):
        if self._conn is None or self._conn.closed:
            self._conn = psycopg2.connect(self.dsn)
            register_vector(self._conn)
        return self._conn

    def _ensure_schema(self) -> None:
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute(_DDL.format(dim=self.dim))
        conn.commit()

    def load(self) -> int:
        self._ensure_schema()
        cols = self.golden_store.list_all()
        for col in cols:
            try:
                self._upsert_db(col, mirror=False)
            except Exception as exc:
                log.warning("pgvector upsert on load failed for %s: %s", col.key(), exc)
        self._count = self.count()
        return self._count

    def _upsert_db(self, col: GoldenColumn, *, mirror: bool = True) -> None:
        if mirror:
            self.golden_store.write(col)
        conn = self._connect()
        sql = """
            INSERT INTO golden_columns (
                table_name, column_name, contract_uuid, embedding, fingerprint,
                classification, entity_type, tags, glossary, synonyms,
                masking_policy, privacy, feature_spec_version, approved_by, approved_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (table_name, column_name) DO UPDATE SET
                contract_uuid = EXCLUDED.contract_uuid,
                embedding = EXCLUDED.embedding,
                fingerprint = EXCLUDED.fingerprint,
                classification = EXCLUDED.classification,
                entity_type = EXCLUDED.entity_type,
                tags = EXCLUDED.tags,
                glossary = EXCLUDED.glossary,
                synonyms = EXCLUDED.synonyms,
                masking_policy = EXCLUDED.masking_policy,
                privacy = EXCLUDED.privacy,
                feature_spec_version = EXCLUDED.feature_spec_version,
                approved_by = EXCLUDED.approved_by,
                approved_at = EXCLUDED.approved_at
        """
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    col.table_name,
                    col.column_name,
                    col.contract_uuid,
                    col.embedding,
                    Json(col.fingerprint),
                    col.classification,
                    col.entity_type,
                    col.tags,
                    col.glossary,
                    col.synonyms,
                    Json(col.masking_policy),
                    Json(col.privacy),
                    col.feature_spec_version,
                    col.approved_by,
                    col.approved_at or None,
                ),
            )
        conn.commit()
        self._count = self.count()

    def upsert(self, col: GoldenColumn) -> None:
        self._upsert_db(col, mirror=True)

    def delete(self, table: str, column: str) -> bool:
        self.golden_store.delete(table, column)
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM golden_columns WHERE table_name = %s AND column_name = %s",
                (table, column),
            )
            deleted = cur.rowcount > 0
        conn.commit()
        self._count = self.count()
        return deleted

    def search(
        self,
        embedding: list[float],
        k: int = 5,
        same_spec_only: bool = True,
        filters: dict | None = None,
    ) -> list[ScoredMatch]:
        from redibis.profiling.fingerprint import FEATURE_SPEC_VERSION
        conn = self._connect()
        where = ["1=1"]
        qparams: list = [embedding]
        if same_spec_only:
            where.append("feature_spec_version = %s")
            qparams.append(FEATURE_SPEC_VERSION)
        if filters and filters.get("classification"):
            where.append("classification = %s")
            qparams.append(filters["classification"])
        qparams.append(k)
        matches: list[ScoredMatch] = []
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT table_name, column_name, contract_uuid, fingerprint,
                       classification, entity_type, tags, glossary, synonyms,
                       masking_policy, privacy, feature_spec_version, approved_by, approved_at,
                       embedding <=> %s::vector AS distance
                FROM golden_columns
                WHERE {' AND '.join(where)}
                ORDER BY distance
                LIMIT %s
                """,
                qparams,
            )
            for row in cur.fetchall():
                col = GoldenColumn(
                    table_name=row[0],
                    column_name=row[1],
                    contract_uuid=str(row[2]) if row[2] else None,
                    embedding=embedding,
                    fingerprint=row[3] if isinstance(row[3], dict) else json.loads(row[3] or "{}"),
                    classification=row[4],
                    entity_type=row[5],
                    tags=list(row[6] or []),
                    glossary=row[7],
                    synonyms=list(row[8] or []),
                    masking_policy=row[9] if isinstance(row[9], dict) else json.loads(row[9] or "{}"),
                    privacy=row[10] if isinstance(row[10], dict) else json.loads(row[10] or "{}"),
                    feature_spec_version=row[11],
                    approved_by=row[12] or "system",
                    approved_at=str(row[13] or ""),
                )
                dist = float(row[14])
                matches.append(ScoredMatch(col, score_from_distance(dist, "cosine"), dist))
        return matches

    def count(self) -> int:
        try:
            conn = self._connect()
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM golden_columns")
                return int(cur.fetchone()[0])
        except Exception:
            return 0

    def health(self) -> dict:
        return {
            "backend": self.name,
            "count": self.count(),
            "ready": _PG_AVAILABLE and bool(self.dsn),
            "dim": self.dim,
        }
