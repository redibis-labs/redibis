"""
redibis.metadata.hive
=====================
Hive Metastore retriever (Tier A) via an injected Spark session.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from redibis.config import ConfigError, SourceConfig
from redibis.metadata.base import (
    CatalogColumnStats,
    ColumnCatalogInfo,
    MetadataRetriever,
    SourceMetadata,
)


class HiveMetastoreRetriever(MetadataRetriever):
    """Read schema and catalog stats from HMS through Spark's catalog API."""

    def __init__(self, config: SourceConfig, *, spark: Any = None) -> None:
        self._config = config
        if spark is None:
            raise ConfigError(
                "Hive metadata retrieval requires an injected Spark session"
            )
        self._spark = spark

    def get_source_metadata(self, table: str) -> SourceMetadata:
        db, tbl = _split_table(table)
        columns = self._list_columns(db, tbl)
        table_props, column_stats = self._describe_formatted(db, tbl, columns)
        return SourceMetadata(
            columns=columns,
            table_props=table_props,
            column_stats=column_stats,
            engine="hive",
        )

    def list_tables(self, database: str = "") -> list[str]:
        catalog = self._spark.catalog
        dbs = [database] if database else [r.name for r in catalog.listDatabases()]
        tables: list[str] = []
        for db in dbs:
            for row in catalog.listTables(db):
                name = getattr(row, "name", None) or row["name"]
                tables.append(f"{db}.{name}" if db else str(name))
        return sorted(tables)

    def list_schemas(self) -> list[str]:
        return sorted(r.name for r in self._spark.catalog.listDatabases())

    def _list_columns(self, db: str, tbl: str) -> list[ColumnCatalogInfo]:
        catalog = self._spark.catalog
        rows = catalog.listColumns(tbl, db or None)
        columns: list[ColumnCatalogInfo] = []
        for idx, row in enumerate(rows):
            name = getattr(row, "name", None) or row["name"]
            dtype = getattr(row, "dataType", None) or row.get("dataType", "string")
            nullable = bool(getattr(row, "nullable", True))
            columns.append(
                ColumnCatalogInfo(
                    name=name,
                    ordinal=idx,
                    native_type=str(dtype),
                    nullable=nullable,
                )
            )
        return columns

    def _describe_formatted(
        self,
        db: str,
        tbl: str,
        columns: list[ColumnCatalogInfo],
    ) -> tuple[dict[str, Any], dict[str, CatalogColumnStats]]:
        qualified = f"{db}.{tbl}" if db else tbl
        df = self._spark.sql(f"DESCRIBE FORMATTED {qualified}")
        rows = [row.asDict(recursive=True) for row in df.collect()]

        table_props: dict[str, Any] = {}
        col_stats: dict[str, CatalogColumnStats] = {
            c.name: CatalogColumnStats(stats_fresh=False) for c in columns
        }

        current_col: Optional[str] = None
        for row in rows:
            col_name = (row.get("col_name") or "").strip()
            data_type = (row.get("data_type") or "").strip()
            comment = (row.get("comment") or "").strip()

            if col_name.startswith("# colname"):
                continue
            if col_name.startswith("#"):
                current_col = None
                continue

            if col_name and data_type and not col_name.startswith("#"):
                if col_name in col_stats:
                    current_col = col_name
                elif current_col is None and col_name in _TABLE_PROP_KEYS:
                    table_props[col_name] = data_type
                continue

            if current_col and col_name:
                self._apply_col_stat(col_stats[current_col], col_name, data_type)
            elif col_name in _TABLE_PROP_KEYS:
                table_props[col_name] = data_type or comment

        last_analyzed = table_props.get("last_analyzed") or table_props.get("Last Access")
        fresh = bool(last_analyzed)
        for stats in col_stats.values():
            if stats.ndv is not None or stats.num_nulls is not None:
                stats.stats_fresh = fresh

        return table_props, col_stats

    def _apply_col_stat(
        self,
        stats: CatalogColumnStats,
        key: str,
        value: str,
    ) -> None:
        key_l = key.lower()
        if key_l in ("numdistinctvalues", "distinct_count", "ndv"):
            stats.ndv = _parse_int(value)
        elif key_l in ("numnulls", "null_count"):
            stats.num_nulls = _parse_int(value)
        elif key_l == "min":
            stats.min = value
        elif key_l == "max":
            stats.max = value
        elif key_l in ("avglength", "avg_col_len"):
            stats.avg_len = _parse_float(value)


_TABLE_PROP_KEYS = frozenset({
    "Owner",
    "Create Time",
    "Last Access",
    "Retention",
    "Location",
    "Table Type",
    "Provider",
    "numRows",
    "totalSize",
    "last_analyzed",
})


def _split_table(table: str) -> tuple[str, str]:
    if "." in table:
        db, tbl = table.split(".", 1)
        return db, tbl
    return "", table


def _parse_int(value: str) -> Optional[int]:
    m = re.search(r"-?\d+", value or "")
    return int(m.group()) if m else None


def _parse_float(value: str) -> Optional[float]:
    m = re.search(r"-?\d+(?:\.\d+)?", value or "")
    return float(m.group()) if m else None
