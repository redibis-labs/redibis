"""
redibis.metadata.jdbc
=====================
Generic JDBC retriever for Postgres / Oracle via information_schema + stats views.
"""

from __future__ import annotations

from typing import Any, Optional

from redibis.config import ConfigError, SourceConfig
from redibis.metadata.base import (
    CatalogColumnStats,
    ColumnCatalogInfo,
    MetadataRetriever,
    SourceMetadata,
)
from redibis.metadata.credentials import resolve_credential_ref


class GenericJdbcRetriever(MetadataRetriever):
    """information_schema columns + engine-specific stats views."""

    def __init__(
        self,
        config: SourceConfig,
        *,
        connection: Any = None,
    ) -> None:
        self._config = config
        self._connection = connection

    def get_source_metadata(self, table: str) -> SourceMetadata:
        engine = (self._config.engine or "jdbc").lower()
        conn = self._connection or self._open_connection(engine)
        db, tbl = _split_table(table)
        schema = db or self._config.jdbc.database or "public"

        columns = self._fetch_columns(conn, engine, schema, tbl)
        table_props = self._fetch_table_props(conn, engine, schema, tbl)
        column_stats = self._fetch_column_stats(conn, engine, schema, tbl, columns)

        return SourceMetadata(
            columns=columns,
            table_props=table_props,
            column_stats=column_stats,
            engine=engine,
        )

    def list_tables(self, database: str = "") -> list[str]:
        engine = (self._config.engine or "jdbc").lower()
        conn = self._connection or self._open_connection(engine)
        schema = (database or "").strip()
        if not schema:
            if engine == "postgres":
                schema = "public"
            elif engine == "oracle":
                schema = self._default_oracle_owner()
            else:
                schema = self._config.jdbc.database or ""
        if engine == "oracle" and not schema:
            raise ConfigError(
                "Oracle owner/schema required — set schema_name or credential username"
            )
        if engine == "oracle":
            sql = (
                "SELECT table_name FROM all_tables "
                "WHERE owner = UPPER(%s) ORDER BY table_name"
            )
        else:
            sql = (
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = %s ORDER BY table_name"
            )
        with conn.cursor() as cur:
            cur.execute(sql, (schema,))
            return [str(r[0]) for r in cur.fetchall()]

    def _default_oracle_owner(self) -> str:
        ref = self._config.jdbc.credential_ref
        if not ref:
            return ""
        try:
            creds = resolve_credential_ref(ref)
            return str(creds.get("username") or creds.get("user") or "")
        except Exception:
            return ""

    def list_schemas(self) -> list[str]:
        engine = (self._config.engine or "jdbc").lower()
        conn = self._connection or self._open_connection(engine)
        if engine == "oracle":
            sql = "SELECT username FROM all_users ORDER BY username"
        else:
            sql = (
                "SELECT schema_name FROM information_schema.schemata "
                "WHERE schema_name NOT LIKE 'pg_%' AND schema_name != 'information_schema' "
                "ORDER BY schema_name"
            )
        with conn.cursor() as cur:
            cur.execute(sql)
            return [str(r[0]) for r in cur.fetchall()]

    def _open_connection(self, engine: str) -> Any:
        jdbc = self._config.jdbc
        if not jdbc.host:
            raise ConfigError("jdbc.host is required for JDBC metadata retrieval")

        creds = resolve_credential_ref(jdbc.credential_ref)
        user = creds.get("username") or creds.get("user") or ""
        password = creds.get("password") or ""

        if engine == "postgres":
            import psycopg2  # type: ignore[import-untyped]

            return psycopg2.connect(
                host=jdbc.host,
                port=jdbc.port or 5432,
                dbname=jdbc.database,
                user=user,
                password=password,
            )

        if engine == "oracle":
            import oracledb  # type: ignore[import-untyped]

            dsn = f"{jdbc.host}:{jdbc.port or 1521}/{jdbc.database}"
            return oracledb.connect(user=user, password=password, dsn=dsn)

        raise ConfigError(
            f"JDBC metadata retrieval for engine {engine!r} requires an injected connection "
            "or a supported driver (postgres, oracle)"
        )

    def _fetch_columns(
        self,
        conn: Any,
        engine: str,
        schema: str,
        table: str,
    ) -> list[ColumnCatalogInfo]:
        if engine == "oracle":
            sql = """
                SELECT column_name, column_id, data_type, nullable
                FROM all_tab_columns
                WHERE owner = UPPER(%s) AND table_name = UPPER(%s)
                ORDER BY column_id
            """
            params = (schema, table)
        else:
            sql = """
                SELECT column_name, ordinal_position, data_type, is_nullable
                FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s
                ORDER BY ordinal_position
            """
            params = (schema, table)

        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

        columns: list[ColumnCatalogInfo] = []
        for row in rows:
            name, ordinal, dtype, nullable_flag = row[0], row[1], row[2], row[3]
            nullable = str(nullable_flag).upper() in ("YES", "Y", "TRUE")
            columns.append(
                ColumnCatalogInfo(
                    name=str(name),
                    ordinal=int(ordinal) - 1,
                    native_type=str(dtype),
                    nullable=nullable,
                )
            )
        return columns

    def _fetch_table_props(
        self,
        conn: Any,
        engine: str,
        schema: str,
        table: str,
    ) -> dict[str, Any]:
        props: dict[str, Any] = {"schema": schema, "table": table}
        if engine == "postgres":
            sql = """
                SELECT reltuples::bigint, pg_total_relation_size(c.oid)
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = %s AND c.relname = %s
            """
            with conn.cursor() as cur:
                cur.execute(sql, (schema, table))
                row = cur.fetchone()
            if row:
                props["row_count"] = row[0]
                props["total_size"] = row[1]
        return props

    def _fetch_column_stats(
        self,
        conn: Any,
        engine: str,
        schema: str,
        table: str,
        columns: list[ColumnCatalogInfo],
    ) -> dict[str, CatalogColumnStats]:
        stats = {c.name: CatalogColumnStats(stats_fresh=False) for c in columns}
        if engine == "postgres":
            sql = """
                SELECT attname, n_distinct, null_frac, avg_width
                FROM pg_stats
                WHERE schemaname = %s AND tablename = %s
            """
            with conn.cursor() as cur:
                cur.execute(sql, (schema, table))
                rows = cur.fetchall()
            for name, n_distinct, null_frac, avg_width in rows:
                if name not in stats:
                    continue
                entry = stats[name]
                entry.avg_len = float(avg_width) if avg_width is not None else None
                if n_distinct is not None and n_distinct >= 0:
                    entry.ndv = int(n_distinct)
                entry.stats_fresh = n_distinct is not None
        elif engine == "oracle":
            sql = """
                SELECT column_name, num_distinct, num_nulls, low_value, high_value
                FROM all_tab_col_statistics
                WHERE owner = UPPER(%s) AND table_name = UPPER(%s)
            """
            with conn.cursor() as cur:
                cur.execute(sql, (schema, table))
                rows = cur.fetchall()
            for name, ndv, num_nulls, low_val, high_val in rows:
                if name not in stats:
                    continue
                entry = stats[name]
                entry.ndv = int(ndv) if ndv is not None else None
                entry.num_nulls = int(num_nulls) if num_nulls is not None else None
                entry.min = low_val
                entry.max = high_val
                entry.stats_fresh = ndv is not None
        return stats


def _split_table(table: str) -> tuple[str, str]:
    if "." in table:
        db, tbl = table.split(".", 1)
        return db, tbl
    return "", table
