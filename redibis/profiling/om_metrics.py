"""
Pandas-native column metrics aligned with OpenMetadata profiler vocabulary.

Uses only pandas/numpy — no OpenMetadata ingestion stack required. When
``openmetadata-ingestion`` is installed, ``OpenMetadataProfiler`` may delegate
here (option b from ARCHITECTURE_PLAN §4).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import pandas as pd

# Column-name tokens where numeric-looking strings should stay ``string``.
_IDENTIFIER_COLUMN_TOKENS: tuple[str, ...] = (
    "id", "zip", "postal", "phone", "mobile", "code", "nid", "national",
    "iban", "account", "ssn", "sku", "serial", "customer_id", "order_id",
)


@dataclass
class ColumnMetric:
    column: str
    data_type: str
    row_count: int = 0
    null_count: int = 0
    null_proportion: float = 0.0
    unique_count: int = 0
    unique_proportion: float = 0.0
    min_value: Optional[Any] = None
    max_value: Optional[Any] = None
    mean: Optional[float] = None
    stddev: Optional[float] = None
    avg_length: Optional[float] = None
    coerced_numeric: bool = False
    coerced_integer: bool = False
    coerced_boolean: bool = False
    coerced_datetime: bool = False
    has_time_component: bool = False
    histogram: list[dict[str, Any]] = field(default_factory=list)
    frequent_values: list[tuple[str, int]] = field(default_factory=list)


def _is_numeric(series: pd.Series) -> bool:
    return pd.api.types.is_numeric_dtype(series)


def _histogram(series: pd.Series, *, bins: int = 10) -> list[dict[str, Any]]:
    clean = series.dropna()
    if clean.empty or not _is_numeric(clean):
        return []
    try:
        counts, edges = pd.cut(clean, bins=min(bins, max(len(clean.unique()), 1)), retbins=True)
        vc = counts.value_counts().sort_index()
        out: list[dict[str, Any]] = []
        for interval, count in vc.items():
            out.append({
                "bucket": str(interval),
                "count": int(count),
            })
        return out
    except Exception:
        return []


def _column_name_suggests_temporal(column: str) -> bool:
    lower = column.lower()
    return any(tok in lower for tok in (
        "_at", "timestamp", "datetime", "created", "updated", "modified",
    )) or lower.endswith("_date") or lower.endswith("_time")


def _column_name_suggests_identifier(column: str) -> bool:
    if _column_name_suggests_temporal(column):
        return False
    parts = set(column.lower().replace("-", "_").split("_"))
    return any(tok in parts for tok in _IDENTIFIER_COLUMN_TOKENS)


def _has_leading_zero_strings(as_str: pd.Series) -> bool:
    """True when any non-null value is numeric-looking with a leading zero."""
    stripped = as_str.astype(str).str.strip()
    return bool(stripped.str.match(r"^0\d+$", na=False).any())


def _is_binary_numeric_flag(as_str: pd.Series) -> bool:
    """Bare 0/1 columns are usually flags, not integers."""
    lowered = as_str.astype(str).str.strip().str.lower()
    return bool(lowered.isin({"0", "1"}).mean() >= 0.95)


def _suppress_string_coercion(column: str, as_str: pd.Series) -> bool:
    """Keep object columns as string despite numeric/boolean content sniffing."""
    return (
        _column_name_suggests_identifier(column)
        or _has_leading_zero_strings(as_str)
        or _is_binary_numeric_flag(as_str)
    )


def _frequent_values(series: pd.Series, *, limit: int = 10) -> list[tuple[str, int]]:
    clean = series.dropna().astype(str)
    if clean.empty:
        return []
    vc = clean.value_counts().head(limit)
    return [(str(k), int(v)) for k, v in vc.items()]


def compute_column_metrics(
    df: pd.DataFrame,
    *,
    max_frequent_values: int = 10,
) -> dict[str, ColumnMetric]:
    """Compute per-column profiler metrics from a DataFrame."""
    if df is None or df.empty:
        return {}

    n_rows = max(len(df), 1)
    metrics: dict[str, ColumnMetric] = {}

    for col in df.columns:
        series = df[col]
        null_count = int(series.isna().sum())
        null_prop = null_count / n_rows
        unique_count = int(series.nunique(dropna=True))
        unique_prop = unique_count / n_rows
        dtype = str(series.dtype)

        min_val = max_val = mean = stddev = avg_len = None
        coerced_numeric = coerced_integer = coerced_boolean = False
        coerced_datetime = has_time_component = False
        if _is_numeric(series):
            clean = series.dropna()
            if not clean.empty:
                min_val = clean.min()
                max_val = clean.max()
                mean = float(clean.mean())
                stddev = float(clean.std()) if len(clean) > 1 else 0.0
        else:
            as_str = series.dropna().astype(str)
            if not as_str.empty:
                lengths = as_str.str.len()
                avg_len = float(lengths.mean())
                if not _is_numeric(series) and not _suppress_string_coercion(col, as_str):
                    try:
                        lowered = as_str.str.strip().str.lower()
                        # Exclude bare 0/1 — too common on identifier / flag columns.
                        bool_set = {"true", "false", "yes", "no"}
                        if lowered.isin(bool_set).mean() >= 0.95:
                            coerced_boolean = True
                        numeric = pd.to_numeric(as_str, errors="coerce")
                        if numeric.notna().mean() >= 0.95:
                            coerced_numeric = True
                            clean = numeric.dropna()
                            min_val = clean.min()
                            max_val = clean.max()
                            mean = float(clean.mean())
                            coerced_integer = bool(
                                (clean % 1 == 0).all() if len(clean) else False
                            )
                        elif not coerced_boolean:
                            parsed = pd.to_datetime(as_str, errors="coerce", utc=False)
                            ratio = parsed.notna().mean()
                            if ratio >= 0.95:
                                coerced_datetime = True
                                valid = parsed.dropna()
                                has_time_component = bool(
                                    valid.dt.time.ne(
                                        pd.Timestamp(0).time()
                                    ).any()
                                ) if len(valid) else False
                    except Exception:
                        pass

        metrics[col] = ColumnMetric(
            column=col,
            data_type=dtype,
            row_count=n_rows,
            null_count=null_count,
            null_proportion=round(null_prop, 6),
            unique_count=unique_count,
            unique_proportion=round(unique_prop, 6),
            min_value=min_val,
            max_value=max_val,
            mean=mean,
            stddev=stddev,
            avg_length=avg_len,
            coerced_numeric=coerced_numeric,
            coerced_integer=coerced_integer,
            coerced_boolean=coerced_boolean,
            coerced_datetime=coerced_datetime,
            has_time_component=has_time_component,
            histogram=_histogram(series),
            frequent_values=_frequent_values(series, limit=max_frequent_values),
        )

    return metrics


def columns_suppress_type_coercion(profiles: list) -> set[str]:
    """PII-triage columns whose OM sniffing should stay ``string``."""
    return {
        p.column
        for p in profiles
        if p.send_to_detector
        and (_column_name_suggests_identifier(p.column) or p.name_hint_score > 0)
    }


def metrics_to_type_hints(
    metrics: dict[str, ColumnMetric],
    *,
    suppress_coercion_for: Optional[set[str]] = None,
) -> dict[str, dict[str, str]]:
    """Map metrics → ``{column: {logical_type, physical_type}}`` for type enrichment."""
    skip = suppress_coercion_for or set()
    hints: dict[str, dict[str, str]] = {}
    for col, m in metrics.items():
        physical = m.data_type
        logical = "string"
        if col in skip:
            pass
        elif _is_numeric_dtype_name(m.data_type):
            logical = "integer" if "int" in m.data_type.lower() else "number"
        elif m.coerced_boolean:
            logical = "boolean"
            physical = "bool"
        elif m.coerced_datetime:
            logical = "timestamp" if m.has_time_component else "date"
            physical = "datetime64[ns]"
        elif m.coerced_numeric:
            logical = "integer" if m.coerced_integer else "number"
            physical = "float64" if not m.coerced_integer else "int64"
        elif "bool" in m.data_type.lower():
            logical = "boolean"
        elif "datetime" in m.data_type.lower():
            logical = "timestamp"
        hints[col] = {"logical_type": logical, "physical_type": physical}
    return hints


def _is_numeric_dtype_name(dtype: str) -> bool:
    key = dtype.lower()
    return any(t in key for t in ("int", "float", "double", "decimal", "number"))
