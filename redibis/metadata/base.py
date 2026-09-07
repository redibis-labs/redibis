"""
redibis.metadata.base
=====================
Tier A catalog metadata — engine-neutral models and retriever ABC.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ColumnCatalogInfo:
    name: str
    ordinal: int
    native_type: str
    nullable: bool = True


@dataclass
class CatalogColumnStats:
    ndv: Optional[int] = None
    num_nulls: Optional[int] = None
    min: Optional[Any] = None
    max: Optional[Any] = None
    avg_len: Optional[float] = None
    histogram: Optional[Any] = None
    stats_fresh: bool = False


@dataclass
class SourceMetadata:
    columns: list[ColumnCatalogInfo] = field(default_factory=list)
    table_props: dict[str, Any] = field(default_factory=dict)
    column_stats: dict[str, CatalogColumnStats] = field(default_factory=dict)
    engine: str = ""


class MetadataRetriever(ABC):
    """Pluggable catalog metadata strategy keyed by ``source.engine``."""

    @abstractmethod
    def get_source_metadata(self, table: str) -> SourceMetadata:
        """Return schema, table properties, and catalog column stats for ``table``."""

    def list_tables(self, database: str = "") -> list[str]:
        """List tables in a database/schema (optional — not all engines implement)."""
        raise NotImplementedError(
            f"{type(self).__name__} does not implement list_tables"
        )
