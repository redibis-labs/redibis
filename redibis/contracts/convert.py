"""Convert ODCS v3.0.x contracts to a portable ODCS v3.1.0 candidate skeleton.

This conversion is used by Contract Synthesis only. It does **not** write
through ``ContractStore.upsert`` and does not mutate active Redibis contracts.
"""

from __future__ import annotations

import copy
from typing import Any, Optional

from redibis.contracts.versions import SYNTHESIS_API_VERSION


_LIBRARY_METRICS = frozenset({
    "nullValues",
    "missingValues",
    "invalidValues",
    "duplicateValues",
    "rowCount",
})

# Legacy / informal metric names → ODCS v3.1 library metrics.
_METRIC_ALIASES = {
    "duplicateCount": "duplicateValues",
    "duplicates": "duplicateValues",
    "null_count": "nullValues",
    "nulls": "nullValues",
    "validValues": "invalidValues",
    "row_count": "rowCount",
}

_OPERATOR_KEYS = (
    "mustBe",
    "mustNotBe",
    "mustBeGreaterThan",
    "mustBeGreaterOrEqualTo",
    "mustBeLessThan",
    "mustBeLessOrEqualTo",
    "mustBeBetween",
    "mustNotBeBetween",
)


def _has_operator(item: dict[str, Any]) -> bool:
    return any(k in item for k in _OPERATOR_KEYS)


def _default_operator(metric: str) -> dict[str, Any]:
    if metric == "rowCount":
        return {"mustBeGreaterThan": 0}
    if metric in {"nullValues", "missingValues", "duplicateValues", "invalidValues"}:
        return {"mustBe": 0}
    return {"mustBe": 0}


def _rule_to_metric(quality_item: dict[str, Any]) -> dict[str, Any]:
    """Normalize a quality item for ODCS v3.1 JSON Schema validation."""
    out = dict(quality_item)
    if "metric" not in out and "rule" in out:
        out["metric"] = out.pop("rule")
    elif "rule" in out and "metric" in out:
        out.pop("rule", None)

    metric = out.get("metric")
    if isinstance(metric, str):
        metric = _METRIC_ALIASES.get(metric, metric)
        out["metric"] = metric

    qtype = str(out.get("type") or "").lower()
    if metric and not qtype:
        out["type"] = "library"
        qtype = "library"

    if qtype == "library":
        if metric not in _LIBRARY_METRICS:
            # Downgrade unknown library metrics to portable text checks.
            desc = str(out.get("description") or metric or "quality check")
            return {
                "type": "text",
                "description": desc,
                **({"name": out["name"]} if out.get("name") else {}),
                **({"severity": out["severity"]} if out.get("severity") else {}),
                **({"dimension": out["dimension"]} if out.get("dimension") else {}),
            }
        if not _has_operator(out):
            out.update(_default_operator(str(metric)))
    return out


def _convert_quality_list(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    out: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict):
            out.append(_rule_to_metric(item))
    return out


# Server keys allowed on the base Server object (type-specific keys come from ServerSource).
_SERVER_BASE_KEYS = frozenset({
    "id", "server", "type", "description", "environment", "roles", "customProperties",
})

# Per-type extra keys (ODCS ServerSource). Unknown keys are moved to customProperties.
_SERVER_TYPE_KEYS: dict[str, frozenset[str]] = {
    "hive": frozenset({"host", "port", "database"}),
    "postgresql": frozenset({"host", "port", "database", "schema"}),
    "postgres": frozenset({"host", "port", "database", "schema"}),
    "snowflake": frozenset({"account", "database", "warehouse", "schema", "role"}),
    "databricks": frozenset({"host", "catalog", "schema"}),
    "bigquery": frozenset({"project", "dataset"}),
    "s3": frozenset({"location", "endpointUrl", "format", "delimiter"}),
    "local": frozenset({"path", "format"}),
    "kafka": frozenset({"host", "format", "topic"}),
}


def _normalize_server(server: dict[str, Any]) -> dict[str, Any]:
    """Drop or relocate keys that fail ODCS v3.1 Server unevaluatedProperties."""
    out = dict(server)
    stype = str(out.get("type") or "").lower()
    allowed = _SERVER_BASE_KEYS | _SERVER_TYPE_KEYS.get(stype, frozenset())
    extras: list[dict[str, Any]] = list(out.get("customProperties") or [])
    for key in list(out.keys()):
        if key not in allowed:
            extras.append({"property": key, "value": out.pop(key)})
    if extras:
        # Deduplicate by property name (last wins).
        by_name: dict[str, Any] = {}
        for item in extras:
            if isinstance(item, dict) and item.get("property"):
                by_name[str(item["property"])] = item
        out["customProperties"] = list(by_name.values())
    return out


def _ensure_id(contract: dict[str, Any]) -> str:
    """Prefer ODCS ``id``, then Redibis ``contract_uuid``; mint only if absent."""
    existing = str(contract.get("id") or contract.get("contract_uuid") or "").strip()
    if existing:
        contract["id"] = existing
        return existing
    import uuid

    minted = str(uuid.uuid4())
    contract["id"] = minted
    return minted


def convert_to_v31(
    contract: dict[str, Any],
    *,
    target_api_version: str = SYNTHESIS_API_VERSION,
) -> dict[str, Any]:
    """
    Return a deep-copied contract upgraded for ODCS v3.1.0 synthesis.

    Changes:
    - set ``apiVersion`` to ``v3.1.0``
    - rename quality ``rule`` → ``metric``
    - ensure stable ``id``
    - strip Redibis-only telemetry keys from the portable export surface
    - ensure empty ``relationships`` arrays exist on schema objects
    """
    if not isinstance(contract, dict):
        raise TypeError("contract must be a mapping")

    out = copy.deepcopy(contract)
    api = str(out.get("apiVersion") or "")
    if api and not api.startswith("v3."):
        raise ValueError(f"cannot convert non-v3 contract apiVersion={api!r}")

    # Telemetry / redibis-only keys must not appear in portable ODCS export.
    for key in (
        "enrichment_meta",
        "provenance",
        "pii_summary",
        "last_updated",
        "last_updated_by_workflow",
        "_scan_metadata",
        "_column_telemetry",
    ):
        out.pop(key, None)

    out["apiVersion"] = target_api_version
    out.setdefault("kind", "DataContract")
    out.setdefault("status", out.get("status") or "draft")
    out.setdefault("version", out.get("version") or "1.0.0")
    _ensure_id(out)

    if isinstance(out.get("servers"), list):
        out["servers"] = [
            _normalize_server(s) if isinstance(s, dict) else s
            for s in out["servers"]
        ]

    for schema_obj in out.get("schema") or []:
        if not isinstance(schema_obj, dict):
            continue
        if "quality" in schema_obj:
            schema_obj["quality"] = _convert_quality_list(schema_obj.get("quality"))
        schema_obj.setdefault("relationships", [])
        for prop in schema_obj.get("properties") or []:
            if not isinstance(prop, dict):
                continue
            if "quality" in prop:
                prop["quality"] = _convert_quality_list(prop.get("quality"))
            # Strip redibis column extensions from portable ODCS export.
            for ext in ("privacy", "pii", "maskingPolicy", "meta"):
                prop.pop(ext, None)
            # ODCS native classification may already be present; leave it.
            prop.setdefault("relationships", [])

    return out


def normalize_portable_contract(contract: dict[str, Any]) -> dict[str, Any]:
    """Re-apply portable ODCS v3.1 normalizations after synthesis stages mutate a candidate."""
    if not isinstance(contract, dict):
        raise TypeError("contract must be a mapping")
    out = contract
    for key in (
        "enrichment_meta",
        "provenance",
        "pii_summary",
        "last_updated",
        "last_updated_by_workflow",
        "_scan_metadata",
        "_column_telemetry",
    ):
        out.pop(key, None)
    if isinstance(out.get("servers"), list):
        out["servers"] = [
            _normalize_server(s) if isinstance(s, dict) else s
            for s in out["servers"]
        ]
    for schema_obj in out.get("schema") or []:
        if not isinstance(schema_obj, dict):
            continue
        if "quality" in schema_obj:
            schema_obj["quality"] = _convert_quality_list(schema_obj.get("quality"))
        for prop in schema_obj.get("properties") or []:
            if isinstance(prop, dict) and "quality" in prop:
                prop["quality"] = _convert_quality_list(prop.get("quality"))
            if isinstance(prop, dict):
                for ext in ("privacy", "pii", "maskingPolicy", "meta"):
                    prop.pop(ext, None)
    return out


def propose_semantic_version_bump(
    base_version: Optional[str],
    *,
    kind: str = "minor",
) -> str:
    """Propose a new semantic version for a synthesis export (does not write)."""
    parts = str(base_version or "1.0.0").split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return "1.1.0" if kind == "minor" else "2.0.0"
    major, minor, patch = (int(p) for p in parts)
    if kind == "major":
        return f"{major + 1}.0.0"
    if kind == "patch":
        return f"{major}.{minor}.{patch + 1}"
    return f"{major}.{minor + 1}.0"
