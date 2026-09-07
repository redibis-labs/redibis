"""
redibis.profiling.pushdown
==========================
Tier B — dialect-aware pushdown aggregates for catalog stat gaps.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator, Optional

from redibis.config import ProfilingConfig, SourceConfig
from redibis.metadata.base import CatalogColumnStats, SourceMetadata
from redibis.metadata.dialect import aggregate_sql


@contextmanager
def _connection_cursor(connection: Any) -> Iterator[Any]:
    """Support psycopg2-style context-manager cursors and plain sqlite3 cursors."""
    cur = connection.cursor()
    if hasattr(cur, "__enter__"):
        with cur as managed:
            yield managed
    else:
        try:
            yield cur
        finally:
            cur.close()


def _needs_pushdown(stats: Optional[CatalogColumnStats]) -> bool:
    if stats is None:
        return True
    if not stats.stats_fresh:
        return True
    if stats.ndv is None and stats.num_nulls is None:
        return True
    return False


def _is_sqlite_connection(connection: Any) -> bool:
    import sqlite3

    return isinstance(connection, sqlite3.Connection)


class PushdownAggregateProfiler:
    """Fill missing or stale catalog stats with live aggregate SQL."""

    def __init__(
        self,
        source_config: SourceConfig,
        profiling_config: ProfilingConfig,
        *,
        spark: Any = None,
        connection: Any = None,
    ) -> None:
        self._source = source_config
        self._profiling = profiling_config
        self._spark = spark
        self._connection = connection
        self._last_sql: list[str] = []

    def _resolve_engine(self, source_metadata: SourceMetadata) -> str:
        """Pick SQL dialect; sqlite3 connections always use the sqlite templates."""
        engine = (source_metadata.engine or self._source.engine or "jdbc").lower()
        if _is_sqlite_connection(self._connection):
            return "sqlite"
        return engine

    @property
    def last_sql(self) -> list[str]:
        """SQL statements from the most recent ``fill_gaps`` call (testing aid)."""
        return list(self._last_sql)

    def fill_gaps(
        self,
        source_metadata: SourceMetadata,
        table: str,
        columns: Optional[list[str]] = None,
    ) -> dict[str, CatalogColumnStats]:
        """
        Query only columns whose catalog stats are missing or ``stats_fresh=False``.
        """
        self._last_sql = []
        engine = self._resolve_engine(source_metadata)
        approx = self._profiling.pushdown.approx_distinct
        col_names = columns or [c.name for c in source_metadata.columns]
        filled: dict[str, CatalogColumnStats] = {}

        for col in col_names:
            existing = source_metadata.column_stats.get(col)
            if not _needs_pushdown(existing):
                continue
            stats = self._query_column(engine, table, col, approx_distinct=approx)
            filled[col] = stats

        return filled

    def _query_column(
        self,
        engine: str,
        table: str,
        column: str,
        *,
        approx_distinct: bool,
    ) -> CatalogColumnStats:
        ndv_sql = aggregate_sql(
            engine,
            "approx_distinct",
            table=table,
            column=column,
            approx_distinct=approx_distinct,
        )
        null_sql = aggregate_sql(engine, "null_count", table=table, column=column)
        minmax_sql = aggregate_sql(engine, "min_max", table=table, column=column)

        ndv = self._scalar(engine, ndv_sql)
        num_nulls = self._scalar(engine, null_sql)
        min_val, max_val = self._pair(engine, minmax_sql)

        self._last_sql.extend([ndv_sql, null_sql, minmax_sql])
        return CatalogColumnStats(
            ndv=int(ndv) if ndv is not None else None,
            num_nulls=int(num_nulls) if num_nulls is not None else None,
            min=min_val,
            max=max_val,
            stats_fresh=True,
        )

    def _scalar(self, engine: str, sql: str) -> Any:
        if engine == "hive":
            if self._spark is None:
                raise RuntimeError("pushdown on hive requires an injected Spark session")
            row = self._spark.sql(sql).collect()[0]
            return row[0]

        if self._connection is None:
            raise RuntimeError("pushdown on JDBC engines requires a connection")
        with _connection_cursor(self._connection) as cur:
            cur.execute(sql)
            row = cur.fetchone()
        return row[0] if row else None

    def _pair(self, engine: str, sql: str) -> tuple[Any, Any]:
        if engine == "hive":
            if self._spark is None:
                raise RuntimeError("pushdown on hive requires an injected Spark session")
            row = self._spark.sql(sql).collect()[0]
            return row[0], row[1]

        if self._connection is None:
            raise RuntimeError("pushdown on JDBC engines requires a connection")
        with _connection_cursor(self._connection) as cur:
            cur.execute(sql)
            row = cur.fetchone()
        if not row:
            return None, None
        return row[0], row[1]
