"""
redibis.metadata
================
Tier A catalog metadata — pluggable retrievers keyed by ``source.engine``.
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
from redibis.metadata.dialect import aggregate_sql, bounded_select_sql, split_qualified_table, validate_sql_identifier
from redibis.metadata.hive import HiveMetastoreRetriever
from redibis.metadata.jdbc import GenericJdbcRetriever

METADATA_RETRIEVER_REGISTRY: dict[str, type[MetadataRetriever]] = {
    "hive": HiveMetastoreRetriever,
    "postgres": GenericJdbcRetriever,
    "oracle": GenericJdbcRetriever,
    "jdbc": GenericJdbcRetriever,
}


def get_metadata_retriever(
    source_config: SourceConfig,
    *,
    spark: Any = None,
    connection: Any = None,
) -> MetadataRetriever:
    engine = (source_config.engine or "none").lower()
    cls = METADATA_RETRIEVER_REGISTRY.get(engine)
    if cls is None:
        raise ConfigError(
            f"unknown source engine {source_config.engine!r}; "
            f"choices: {sorted(METADATA_RETRIEVER_REGISTRY)}"
        )
    if engine == "hive":
        return cls(source_config, spark=spark)
    return cls(source_config, connection=connection)


__all__ = [
    "CatalogColumnStats",
    "ColumnCatalogInfo",
    "MetadataRetriever",
    "SourceMetadata",
    "METADATA_RETRIEVER_REGISTRY",
    "HiveMetastoreRetriever",
    "GenericJdbcRetriever",
    "aggregate_sql",
    "bounded_select_sql",
    "split_qualified_table",
    "validate_sql_identifier",
    "get_metadata_retriever",
]
