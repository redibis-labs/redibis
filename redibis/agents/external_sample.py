"""Load bounded sample rows from external sources (Hive Spark / JDBC)."""

from __future__ import annotations

from typing import Any, Optional

import pandas as pd

from redibis.config import SourceConfig
from redibis.services.source_sample import load_table_sample


def load_external_sample(
    source_config: SourceConfig,
    table: str,
    *,
    rows: int = 5000,
    spark: Any = None,
) -> pd.DataFrame:
    """Return up to ``rows`` sample rows for ``table`` from a live external source."""
    return load_table_sample(
        table,
        source_config=source_config,
        rows=rows,
        spark=spark,
    )


__all__ = ["load_external_sample"]
