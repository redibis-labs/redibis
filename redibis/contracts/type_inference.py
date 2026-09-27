"""
redibis.contracts.type_inference — pandas dtype → ODCS logicalType / physicalType.

Centralises type mapping so quality and PII writers emit accurate types instead of
defaulting every column to ``logicalType: "string"``.
"""

from __future__ import annotations

from typing import Any, Optional


def infer_types_from_dtype(dtype: Any) -> tuple[str, str]:
    """Map a pandas / numpy dtype to ``(physicalType, logicalType)``.

    ``physicalType`` is the concrete storage primitive; ``logicalType`` is the
    ODCS semantic type downstream consumers rely on (decimal for money, timestamp
    for datetimes, etc.).
    """
    import pandas as pd

    name = str(dtype)
    lower = name.lower()

    if pd.api.types.is_bool_dtype(dtype):
        return "boolean", "boolean"
    if pd.api.types.is_integer_dtype(dtype):
        return name, "integer"
    if pd.api.types.is_float_dtype(dtype):
        return name, "number"
    if pd.api.types.is_datetime64_any_dtype(dtype):
        return "timestamp", "timestamp"
    if pd.api.types.is_timedelta64_dtype(dtype):
        return "interval", "interval"
    try:
        from pandas import CategoricalDtype
        if isinstance(dtype, CategoricalDtype):
            return "category", "string"
    except ImportError:
        pass
    if pd.api.types.is_string_dtype(dtype) or pd.api.types.is_object_dtype(dtype):
        return "string", "string"

    # Fallback substring heuristics (Spark / odd dtype strings).
    # Complex Spark types first: "map<string,int>" must not match "int".
    if lower.startswith("array<"):
        return name, "array"
    if lower.startswith(("map<", "struct<")):
        return name, "object"
    if any(x in lower for x in ("decimal", "numeric", "float", "double")):
        return name, "decimal" if "decimal" in lower else "number"
    if any(x in lower for x in ("int", "long", "short", "bigint")):
        return name, "integer"
    if any(x in lower for x in ("timestamp", "datetime", "date")):
        return "timestamp" if "timestamp" in lower or "datetime" in lower else "date", (
            "timestamp" if "timestamp" in lower or "datetime" in lower else "date"
        )
    if "bool" in lower:
        return "boolean", "boolean"
    return "string", "string"


def infer_types_from_series(series) -> tuple[str, str]:
    """Infer types from a pandas Series (uses ``series.dtype``)."""
    return infer_types_from_dtype(series.dtype)


def apply_inferred_types(prop: dict, dtype: Any) -> dict:
    """Set ``physicalType`` / ``logicalType`` on a column property dict in place."""
    physical, logical = infer_types_from_dtype(dtype)
    prop["physicalType"] = physical
    prop["logicalType"] = logical
    return prop


def is_spark_dataframe(df) -> bool:
    """True for a ``pyspark.sql.DataFrame`` (checked without importing pyspark)."""
    return hasattr(df, "sparkSession") and hasattr(df, "schema")


def dtype_map_from_dataframe(df) -> dict[str, Any]:
    """``{column_name: dtype}`` from a sampled DataFrame (pandas or Spark).

    Spark columns map to their SQL type string (``bigint``, ``decimal(10,2)``…),
    which ``infer_types_from_dtype`` understands via its substring fallback.
    """
    if df is None:
        return {}
    if is_spark_dataframe(df):
        return {str(f.name): f.dataType.simpleString() for f in df.schema.fields}
    try:
        return {str(c): df[c].dtype for c in df.columns}
    except Exception:
        return {}
