"""
redibis.contracts.schema_base — full-table ODCS schema backbone.

Every contract write path should upsert this partial first (workflow ``schema``)
so PII / quality / business enrichments merge onto a complete column set.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from redibis.contracts.type_inference import infer_types_from_dtype
from redibis.services.session.state import split_table


_ALWAYS_STRING_PARTS = frozenset({
    "phone", "mobile", "zip", "postal", "nid", "national", "iban", "ssn", "sku", "serial",
})


def _identifier_string_columns(col_dtypes: Mapping[str, Any]) -> set[str]:
    """Identifier-named columns that must stay ``string`` (even when CSV parsed them as int)."""
    try:
        import pandas as pd
        from redibis.profiling.om_metrics import _column_name_suggests_identifier
    except ImportError:
        return set()
    forced: set[str] = set()
    for col, dtype in col_dtypes.items():
        parts = set(col.lower().replace("-", "_").split("_"))
        if parts & _ALWAYS_STRING_PARTS:
            forced.add(col)
            continue
        if not _column_name_suggests_identifier(col):
            continue
        if pd.api.types.is_integer_dtype(dtype) and col.lower() in ("id", "idx", "pk"):
            continue
        if pd.api.types.is_object_dtype(dtype) or pd.api.types.is_string_dtype(dtype):
            forced.add(col)
    return forced


def _resolve_column_types(
    col_dtypes: Mapping[str, Any],
    *,
    df=None,
    profiles: Optional[Sequence[Any]] = None,
) -> dict[str, dict[str, str]]:
    """Return ``{column: {logicalType, physicalType?}}`` with content sniffing when possible."""
    hints: dict[str, dict[str, str]] = {}
    if df is not None:
        try:
            from redibis.profiling.om_metrics import (
                columns_suppress_type_coercion,
                compute_column_metrics,
                metrics_to_type_hints,
            )

            metrics = compute_column_metrics(df)
            suppress = columns_suppress_type_coercion(list(profiles or []))
            suppress |= _identifier_string_columns(col_dtypes)
            raw = metrics_to_type_hints(metrics, suppress_coercion_for=suppress)
            for col, h in raw.items():
                logical = h.get("logical_type", "string")
                physical = h.get("physical_type", "string")
                hints[col] = {"logicalType": logical}
                if physical and physical != "string" and logical != physical:
                    hints[col]["physicalType"] = physical
        except Exception:
            hints = {}

    for col, dtype in col_dtypes.items():
        if col in hints:
            continue
        physical, logical = infer_types_from_dtype(dtype)
        entry: dict[str, str] = {"logicalType": logical}
        if physical and physical != "string" and logical != physical:
            entry["physicalType"] = physical
        hints[col] = entry

    for col in _identifier_string_columns(col_dtypes):
        hints[col] = {"logicalType": "string", "physicalType": "string"}

    return hints


def build_schema_base(
    table: str,
    col_dtypes: Mapping[str, Any],
    *,
    df=None,
    profiles: Optional[Sequence[Any]] = None,
    column_definitions: Optional[Mapping[str, Mapping[str, Any]]] = None,
    table_description: Optional[str] = None,
) -> dict:
    """Build an ODCS partial with one typed property per table column."""
    db_name, tbl_name = split_table(table)
    model_name = f"{db_name}_{tbl_name}"
    type_map = _resolve_column_types(col_dtypes, df=df, profiles=profiles)
    col_defs = column_definitions or {}

    properties = []
    for col in sorted(col_dtypes.keys(), key=str):
        types = type_map.get(col, {"logicalType": "string"})
        prop: dict[str, Any] = {"name": col, **types}
        extra = col_defs.get(col) or {}
        for key in ("description", "businessName"):
            if extra.get(key):
                prop[key] = extra[key]
        if extra.get("meta"):
            prop["meta"] = dict(extra["meta"])
        properties.append(prop)

    schema_obj: dict[str, Any] = {
        "name": model_name,
        "physicalName": table,
        "properties": properties,
    }
    if table_description:
        schema_obj["description"] = table_description

    return {
        "apiVersion": "v3.0.1",
        "kind": "DataContract",
        "name": f"{model_name}_contract",
        "version": "1.0.0",
        "status": "active",
        "database_name": db_name,
        "table_name": tbl_name,
        "schema": [schema_obj],
    }


def missing_schema_columns(contract: Optional[dict], expected_columns: Sequence[str]) -> set[str]:
    """Columns present in the profile/sample but absent from the active contract."""
    if not expected_columns:
        return set()
    existing: set[str] = set()
    if contract:
        for schema_obj in contract.get("schema", []) or []:
            for prop in schema_obj.get("properties", []) or []:
                name = prop.get("name")
                if name:
                    existing.add(str(name))
    return set(expected_columns) - existing
