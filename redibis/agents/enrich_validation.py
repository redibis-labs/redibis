"""Shared enrich output safety checks (validator + tool runner)."""

from __future__ import annotations

from typing import Any


def iter_contract_columns(contract: dict):
    for schema_obj in contract.get("schema", []) or []:
        for prop in schema_obj.get("properties", []) or []:
            if isinstance(prop, dict):
                yield prop


def enrich_safety_errors(contract: dict) -> list[str]:
    from redibis.contracts.privacy import column_is_pii

    errors: list[str] = []
    if "enrichment_meta" in contract:
        errors.append("enrichment_meta must not appear in active contract spec")
    for col in iter_contract_columns(contract):
        name = col.get("name") or "?"
        if column_is_pii(col) and col.get("quality"):
            errors.append(f"PII column {name!r} must not carry quality rules in spec")
    return errors


def enrich_output_validation_fields(
    *,
    result_valid: bool,
    result_errors: list[str],
    enrichment_meta: dict[str, Any],
    candidate: dict,
) -> dict[str, Any]:
    """Build validator-facing fields from an EnrichmentResult-shaped payload."""
    slim = dict(candidate or {})
    slim.pop("enrichment_meta", None)
    return {
        "valid": result_valid,
        "validation_errors": list(result_errors),
        "artifact_odcs_errors": list(enrichment_meta.get("artifact_odcs_errors") or []),
        "safety_errors": enrich_safety_errors(slim) if slim else [],
        "validation_reported": True,
    }
