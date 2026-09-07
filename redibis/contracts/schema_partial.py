"""
redibis.contracts.schema_partial — backward-compatible alias for schema base builder.

Prefer ``redibis.contracts.schema_base.build_schema_base`` for new code.
"""

from __future__ import annotations

from typing import Any, Mapping

from redibis.contracts.schema_base import build_schema_base


def build_schema_partial(
    table: str,
    col_dtypes: Mapping[str, Any],
    *,
    df=None,
) -> dict:
    """Build an ODCS partial with one property per column (types only)."""
    return build_schema_base(table, col_dtypes, df=df)
