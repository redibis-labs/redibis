"""
redibis.profiling.metadata_tiers
=================================
Compose Tier A (catalog) + Tier B (pushdown) around Tier C sample profiling.
"""

from __future__ import annotations

from typing import Any, Optional

import pandas as pd

from redibis.config import ProfilingConfig, SourceConfig
from redibis.memory.fingerprint import build_fingerprints
from redibis.metadata import get_metadata_retriever
from redibis.metadata.base import SourceMetadata
from redibis.profiling.base import ProfileResult
from redibis.profiling.pushdown import PushdownAggregateProfiler


def enrich_profile_with_metadata(
    result: ProfileResult,
    df: pd.DataFrame,
    *,
    table: str,
    profiling: ProfilingConfig,
    source: SourceConfig,
    domain: str = "",
    spark: Any = None,
    connection: Any = None,
) -> ProfileResult:
    """
    When ``profiling.metadata.enabled``, attach catalog fingerprints to ``result``.

    With ``profiling.pushdown.enabled``, fill stale catalog stat gaps first.
    """
    if not profiling.metadata.enabled:
        return result

    retriever = get_metadata_retriever(source, spark=spark, connection=connection)
    source_metadata = retriever.get_source_metadata(table)

    gap_stats = {}
    if profiling.pushdown.enabled:
        pushdown = PushdownAggregateProfiler(
            source,
            profiling,
            spark=spark,
            connection=connection,
        )
        gap_stats = pushdown.fill_gaps(source_metadata, table)
        for col, stats in gap_stats.items():
            source_metadata.column_stats[col] = stats

    result.source_metadata = source_metadata
    result.fingerprints = build_fingerprints(
        df,
        source_metadata,
        result.column_profiles,
        table=table,
        domain=domain,
        gap_stats=gap_stats,
    )
    result.raw["source_metadata"] = source_metadata
    result.raw["metadata_engine"] = source.engine
    return result


def apply_catalog_props_to_partial(partial: dict, source_metadata: SourceMetadata) -> dict:
    """Merge Tier-A table props (retention, owner) into an ODCS partial."""
    props = source_metadata.table_props or {}
    owner = props.get("Owner") or props.get("owner")
    if owner:
        partial["owner"] = owner

    retention = props.get("Retention") or props.get("retention")
    if retention is not None:
        from redibis.contracts.retention import set_retention

        try:
            value = int(str(retention).strip())
            set_retention(partial, value, unit="d", driver="catalog")
        except (TypeError, ValueError):
            set_retention(partial, str(retention), unit="d", driver="catalog")

    return partial
