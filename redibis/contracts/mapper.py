"""
redibis.contracts.mapper
========================
Maps GE ExpectationConfiguration objects to ODCS DataQuality objects.

Two output tiers:
  1. Native ODCS rules (portable) — when the GE expectation has a direct equivalent.
     These work with Soda, dbt, and any ODCS-aware engine.
  2. Engine-specific (GE only) — when there's no ODCS equivalent.
     These pass through to GE but are ignored by other engines.
"""

from __future__ import annotations

from typing import Any, Optional

try:
    from open_data_contract_standard.model import DataQuality
    _ODCS_AVAILABLE = True
except ImportError:
    _ODCS_AVAILABLE = False
    DataQuality = None


def ge_expectation_to_odcs(expectation: Any) -> dict:
    """
    Convert a single GE ExpectationConfiguration to an ODCS DataQuality dict.

    Returns a native ODCS rule dict if the expectation has a direct mapping,
    otherwise returns an engine-specific GE custom block.

    Works regardless of whether open-data-contract-standard is installed —
    always returns a plain dict. If ODCS models are available, validates
    through DataQuality construction.

    Args:
        expectation: A GE ExpectationConfiguration object with
                     .expectation_type (str) and .kwargs (dict).

    Returns:
        dict suitable for inclusion in an ODCS quality[] list.
    """
    exp_type = getattr(expectation, "expectation_type", None)
    if exp_type is None and isinstance(expectation, dict):
        exp_type = expectation.get("expectation_type", expectation.get("type"))
    if exp_type is None:
        return _engine_specific(str(expectation), {})

    kwargs = dict(getattr(expectation, "kwargs", {}))
    kwargs.pop("batch_id", None)
    meta = getattr(expectation, "meta", {}) or {}

    mapper = _NATIVE_MAP.get(exp_type)
    if mapper:
        result = mapper(kwargs)
    else:
        result = _engine_specific(exp_type, kwargs)

    # Preserve GE meta notes as ODCS description
    if meta.get("notes", {}).get("content"):
        result["description"] = meta["notes"]["content"]

    # Validate through Pydantic if available (preserve redibis extensions ODCS drops)
    if _ODCS_AVAILABLE and DataQuality is not None:
        try:
            dq = DataQuality(**result)
            validated = dq.model_dump(exclude_none=True)
            for key, val in result.items():
                if key not in validated and val is not None:
                    validated[key] = val
            return validated
        except Exception:
            pass

    return {k: v for k, v in result.items() if v is not None}


def sql_rule_to_odcs(
    query: str,
    max_failures: int = 0,
    description: Optional[str] = None,
) -> dict:
    """Convert an enforce_sql_rule call to an ODCS SQL quality check."""
    result = {
        "type": "sql",
        "query": query,
        "mustBeLessThan": max_failures + 1,
    }
    if description:
        result["description"] = description
    return result


def ge_expectations_to_odcs(expectations: list) -> list[dict]:
    """Convert a list of GE expectations to ODCS DataQuality dicts."""
    return [ge_expectation_to_odcs(exp) for exp in expectations]


# ── Native mappings (portable across engines) ─────────────────────────────

def _map_not_null(kwargs: dict) -> dict:
    """expect_column_values_to_not_be_null → missingCount."""
    mostly = kwargs.get("mostly", 1.0)
    if mostly >= 1.0:
        return {"rule": "missingCount", "mustBe": 0}
    max_missing_pct = round((1.0 - mostly) * 100, 2)
    return {"rule": "missingCount", "mustBeLessThan": max_missing_pct, "unit": "percent"}


def _map_null(kwargs: dict) -> dict:
    """expect_column_values_to_be_null → missingCount must equal row count (all null)."""
    return {"rule": "missingCount", "mustBe": 100, "unit": "percent"}


def _map_unique(kwargs: dict) -> dict:
    """expect_column_values_to_be_unique → duplicateCount."""
    mostly = kwargs.get("mostly", 1.0)
    if mostly >= 1.0:
        return {"rule": "duplicateCount", "mustBe": 0}
    max_dup_pct = round((1.0 - mostly) * 100, 2)
    return {"rule": "duplicateCount", "mustBeLessThan": max_dup_pct, "unit": "percent"}


def _map_row_count_between(kwargs: dict) -> dict:
    """expect_table_row_count_to_be_between → rowCount."""
    lo = kwargs.get("min_value", 0)
    hi = kwargs.get("max_value")
    if hi is not None:
        return {"rule": "rowCount", "mustBeBetween": [lo, hi]}
    return {"rule": "rowCount", "mustBeGreaterOrEqualTo": lo}


def _map_row_count_equal(kwargs: dict) -> dict:
    """expect_table_row_count_to_equal → rowCount."""
    return {"rule": "rowCount", "mustBe": kwargs.get("value")}


def _map_valid_values(kwargs: dict) -> dict:
    """expect_column_values_to_be_in_set → validValues."""
    return {
        "rule": "validValues",
        "arguments": {"validValues": kwargs.get("value_set", [])},
    }


def _map_unique_count_between(kwargs: dict) -> dict:
    """expect_column_unique_value_count_to_be_between → uniqueCount."""
    lo = kwargs.get("min_value", 0)
    hi = kwargs.get("max_value")
    if hi is not None:
        return {"rule": "uniqueCount", "mustBeBetween": [lo, hi]}
    return {"rule": "uniqueCount", "mustBeGreaterOrEqualTo": lo}


def _map_match_regex(kwargs: dict) -> dict:
    """
    expect_column_values_to_match_regex → portable regex format rule.

    Used for E.164 MSISDN, 15-digit IMSI, and any other format constraint.
    Round-trips back to GE via redibis.contracts.rules._rule_to_ge (regex branch).
    """
    rule: dict = {"rule": "regex", "pattern": kwargs.get("regex", "")}
    mostly = kwargs.get("mostly")
    if mostly is not None and mostly < 1.0:
        # fraction of values allowed to fail the format, expressed as percent
        rule["mustBeGreaterOrEqualTo"] = round(mostly * 100, 2)
        rule["unit"] = "percent"
    return rule


# ── Engine-specific fallback ──────────────────────────────────────────────

def _engine_specific(exp_type: str, kwargs: dict) -> dict:
    """Wrap a GE expectation as an engine-specific ODCS custom block."""
    return {
        "engine": "greatExpectations",
        "implementation": {
            "expectation_type": exp_type,
            "kwargs": kwargs,
        },
    }


# ── Dispatch table ────────────────────────────────────────────────────────

_NATIVE_MAP = {
    "expect_column_values_to_not_be_null":          _map_not_null,
    "expect_column_values_to_be_null":              _map_null,
    "expect_column_values_to_be_unique":            _map_unique,
    "expect_table_row_count_to_be_between":         _map_row_count_between,
    "expect_table_row_count_to_equal":              _map_row_count_equal,
    "expect_column_values_to_be_in_set":            _map_valid_values,
    "expect_column_unique_value_count_to_be_between": _map_unique_count_between,
    "expect_column_values_to_match_regex":          _map_match_regex,
}