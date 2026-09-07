"""Strict ODCS validation for Contract Synthesis (portable v3.1 export)."""

from __future__ import annotations

import copy
import re
from typing import Any, Optional

from redibis.contracts.odcs_compat import prepare_for_odcs_pydantic
from redibis.contracts.versions import SYNTHESIS_API_VERSION, get_profile, normalize_api_version


def validate_odcs_contract(
    contract: dict[str, Any],
    *,
    api_version: Optional[str] = None,
    strict: bool = True,
) -> dict[str, Any]:
    """
    Validate a contract against the official ODCS JSON Schema for ``api_version``.

    Returns ``{valid, errors, warnings, api_version}``.
    When ``strict`` is True and errors exist, raises ``ValueError``.
    """
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(contract, dict):
        errors.append("contract must be a mapping")
        result = {
            "valid": False,
            "errors": errors,
            "warnings": warnings,
            "api_version": api_version or "",
        }
        if strict:
            raise ValueError("ODCS validation failed:\n" + "\n".join(f"  - {e}" for e in errors))
        return result

    target = normalize_api_version(api_version or contract.get("apiVersion"))
    profile = get_profile(target)
    if not profile.enabled:
        errors.append(f"ODCS profile {target} is disabled: {profile.notes}")
        result = {
            "valid": False,
            "errors": errors,
            "warnings": warnings,
            "api_version": target,
        }
        if strict:
            raise ValueError("ODCS validation failed:\n" + "\n".join(f"  - {e}" for e in errors))
        return result

    clean = prepare_for_odcs_pydantic(contract)
    # Ensure apiVersion matches the profile under test.
    clean = copy.deepcopy(clean)
    clean["apiVersion"] = target

    # Layer 1: official JSON Schema (preferred for synthesis).
    try:
        import json

        import jsonschema
        from jsonschema import Draft201909Validator

        schema = json.loads(profile.schema_path().read_text(encoding="utf-8"))
        validator = Draft201909Validator(schema)
        for err in sorted(validator.iter_errors(clean), key=lambda e: list(e.path)):
            path = "/".join(str(p) for p in err.path) or "(root)"
            errors.append(f"{path}: {err.message}")
    except ImportError:
        warnings.append(
            "jsonschema not installed — falling back to open-data-contract-standard Pydantic"
        )
        try:
            from redibis.contracts.odcs_compat import build_odcs_model

            build_odcs_model(clean)
        except Exception as exc:
            errors.append(f"ODCS model validation: {exc}")
    except Exception as exc:
        errors.append(f"ODCS JSON Schema validation: {exc}")

    # Soft telemetry guard (portable artifacts must not carry run telemetry).
    for key in (
        "enrichment_meta",
        "provenance",
        "pii_summary",
        "last_updated",
        "last_updated_by_workflow",
        "_scan_metadata",
    ):
        if key in contract:
            errors.append(f"telemetry key {key!r} must not appear in portable contract")

    valid = len(errors) == 0
    result = {
        "valid": valid,
        "errors": errors,
        "warnings": warnings,
        "api_version": target,
    }
    if strict and not valid:
        raise ValueError(
            "ODCS validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
        )
    return result


def assert_synthesis_contract(contract: dict[str, Any]) -> list[str]:
    """Validate a synthesis candidate as ODCS v3.1.0; return errors (no raise)."""
    result = validate_odcs_contract(
        contract,
        api_version=SYNTHESIS_API_VERSION,
        strict=False,
    )
    return list(result.get("errors") or [])


def is_odcs_v3(api_version: str) -> bool:
    return bool(re.match(r"^v3\.\d+", str(api_version or "")))
