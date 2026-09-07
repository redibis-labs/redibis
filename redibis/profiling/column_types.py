"""
Column type inference for profiling reports.

Baseline types come from pandas (engine-neutral). Profiler backends may override
via ``enrich_profiles_from_ge`` / ``enrich_profiles_from_openmetadata`` (Phase 5).
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from redibis.contracts.type_inference import infer_types_from_dtype, infer_types_from_series
from redibis.models import ColumnProfile

# GE ``type_`` values → ODCS logicalType (best-effort)
_GE_LOGICAL_TYPE_MAP: dict[str, str] = {
    "string": "string",
    "str": "string",
    "integer": "integer",
    "int": "integer",
    "float": "number",
    "double": "number",
    "number": "number",
    "boolean": "boolean",
    "bool": "boolean",
    "datetime": "timestamp",
    "date": "date",
    "timestamp": "timestamp",
    "timedelta": "interval",
    "dict": "object",
    "object": "object",
    "list": "array",
    "array": "array",
}


def infer_column_types_from_series(series) -> tuple[str, str, str]:
    """
    Infer ``(physical_type, logical_type, type_source)`` from a pandas Series.

    ``type_source`` is ``"pandas"`` until a profiler engine overrides it.
    """
    physical, logical = infer_types_from_series(series)
    return physical, logical, "pandas"


def infer_column_types_from_dtype(dtype: Any) -> tuple[str, str, str]:
    physical, logical = infer_types_from_dtype(dtype)
    return physical, logical, "pandas"


def _normalize_ge_type(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    key = str(raw).strip().lower()
    return _GE_LOGICAL_TYPE_MAP.get(key)


def _ge_type_for_column(expectations: Iterable[Any], column: str) -> Optional[str]:
    """Best-effort logical type from GE expectations targeting ``column``."""
    found: Optional[str] = None
    for exp in expectations or []:
        kwargs = getattr(exp, "kwargs", None) or {}
        if kwargs.get("column") != column:
            continue
        etype = getattr(exp, "expectation_type", "") or ""
        if etype == "expect_column_values_to_be_of_type":
            mapped = _normalize_ge_type(kwargs.get("type_") or kwargs.get("type"))
            if mapped:
                found = mapped
        elif etype == "expect_column_values_to_be_in_type_list":
            type_list = kwargs.get("type_list") or []
            for raw in type_list:
                mapped = _normalize_ge_type(raw)
                if mapped and mapped != "string":
                    found = mapped
                    break
    return found


def enrich_profiles_from_ge(
    profiles: list[ColumnProfile],
    expectations: Iterable[Any],
) -> list[ColumnProfile]:
    """
    Override ``logical_type`` / ``type_source`` where GE suggests a semantic type.

    Returns a new list (does not mutate inputs).
    """
    out: list[ColumnProfile] = []
    for p in profiles:
        ge_logical = _ge_type_for_column(expectations, p.column)
        if ge_logical and ge_logical != p.logical_type:
            out.append(
                ColumnProfile(
                    column=p.column,
                    dtype=p.dtype,
                    physical_type=p.physical_type,
                    logical_type=ge_logical,
                    type_source="great_expectations",
                    cardinality_ratio=p.cardinality_ratio,
                    avg_value_length=p.avg_value_length,
                    null_rate=p.null_rate,
                    name_hint_score=p.name_hint_score,
                    arabic_fraction=p.arabic_fraction,
                    triage_score=p.triage_score,
                    send_to_detector=p.send_to_detector,
                    sample_values=list(p.sample_values),
                )
            )
        else:
            out.append(p)
    return out


def enrich_profiles_from_openmetadata(
    profiles: list[ColumnProfile],
    om_types: dict[str, dict[str, str]],
) -> list[ColumnProfile]:
    """
    Override types from OpenMetadata profiler metrics (Phase 5).

    ``om_types`` shape: ``{column: {"logical_type": "...", "physical_type": "..."}}``.
    """
    out: list[ColumnProfile] = []
    for p in profiles:
        hint = om_types.get(p.column) or {}
        logical = hint.get("logical_type") or p.logical_type
        physical = hint.get("physical_type") or p.physical_type
        if hint:
            out.append(
                ColumnProfile(
                    column=p.column,
                    dtype=p.dtype,
                    physical_type=physical,
                    logical_type=logical,
                    type_source="open_metadata",
                    cardinality_ratio=p.cardinality_ratio,
                    avg_value_length=p.avg_value_length,
                    null_rate=p.null_rate,
                    name_hint_score=p.name_hint_score,
                    arabic_fraction=p.arabic_fraction,
                    triage_score=p.triage_score,
                    send_to_detector=p.send_to_detector,
                    sample_values=list(p.sample_values),
                )
            )
        else:
            out.append(p)
    return out


def format_type_cell(profile: ColumnProfile) -> str:
    """HTML cell for triage reports: logical type + physical/source tooltip."""
    title = f"physical: {profile.physical_type} · source: {profile.type_source}"
    return (
        f"<td title=\"{title}\">"
        f"<span style='font-weight:600'>{profile.logical_type}</span>"
        f"<span style='font-size:10px;color:#64748b;display:block'>"
        f"{profile.physical_type}</span></td>"
    )
