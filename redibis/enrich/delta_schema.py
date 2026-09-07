"""
Pydantic schema for the enrichment LLM delta (structured output validation).

Lightweight — no LangChain dependency. Used before ``apply_enrichment`` so
malformed model output is repaired or rejected instead of corrupting the contract.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ColumnBusinessDelta(BaseModel):
    model_config = ConfigDict(extra="ignore")

    definition: Optional[str] = None
    synonyms: Optional[list[str]] = None
    example_values: Optional[list[str]] = None
    tags: Optional[list[str]] = None


class ColumnPiiDelta(BaseModel):
    model_config = ConfigDict(extra="ignore")

    classification: Optional[str] = None
    entity_type: Optional[str] = None
    reason: Optional[str] = None

    @field_validator("classification")
    @classmethod
    def _norm_classification(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        return str(v).strip()


class ColumnEnrichmentDelta(BaseModel):
    model_config = ConfigDict(extra="ignore")

    businessName: Optional[str] = None
    business: Optional[ColumnBusinessDelta] = None
    pii: Optional[ColumnPiiDelta] = None
    tags: Optional[list[str]] = None


class TableBusinessDelta(BaseModel):
    model_config = ConfigDict(extra="ignore")

    description: Optional[str] = None
    purpose: Optional[str] = None


class EnrichmentDelta(BaseModel):
    model_config = ConfigDict(extra="ignore")

    table: Optional[TableBusinessDelta] = None
    table_tags: Optional[list[str]] = None
    columns: dict[str, ColumnEnrichmentDelta] = Field(default_factory=dict)


def enrichment_delta_json_schema() -> dict[str, Any]:
    """JSON schema for provider ``response_format`` when supported."""
    return EnrichmentDelta.model_json_schema()


def parse_enrichment_delta(raw: Any) -> tuple[dict[str, Any], list[str]]:
    """
    Validate/repair an LLM enrichment delta.

    Returns ``(delta_dict, errors)``. When errors is non-empty the delta may
    still be partially usable (unknown keys dropped via ``extra='ignore'``).
    Completely invalid payloads return ``({}, errors)``.
    """
    errors: list[str] = []
    if raw is None:
        return {}, ["delta is empty"]
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            return {}, [f"delta JSON parse failed: {exc}"]
    if not isinstance(raw, dict):
        return {}, ["delta must be a JSON object"]

    allowed_top = {"table", "table_tags", "columns"}
    known_stripped = {"review"}
    for key in raw:
        if key not in allowed_top and key not in known_stripped:
            errors.append(f"dropped unknown top-level key: {key!r}")

    payload = {k: raw[k] for k in allowed_top if k in raw}
    columns_raw = payload.get("columns")
    if columns_raw is not None and not isinstance(columns_raw, dict):
        errors.append("columns must be an object")
        payload["columns"] = {}

    try:
        model = EnrichmentDelta.model_validate(payload)
    except Exception as exc:
        errors.append(f"delta schema validation failed: {exc}")
        return {}, errors

    out: dict[str, Any] = {}
    if model.table is not None:
        out["table"] = model.table.model_dump(exclude_none=True)
    if model.table_tags is not None:
        out["table_tags"] = model.table_tags
    out["columns"] = {
        col: cd.model_dump(exclude_none=True)
        for col, cd in (model.columns or {}).items()
    }
    return out, errors


def filter_delta_for_stage(delta: dict[str, Any], stage_kind: str) -> dict[str, Any]:
    """Keep only fields allowed for a prebuilt enrichment stage."""
    kind = (stage_kind or "full").strip().lower()
    if kind in ("full", "all", ""):
        return dict(delta)
    out: dict[str, Any] = {}
    if kind == "column_definitions":
        cols = {}
        for name, cd in (delta.get("columns") or {}).items():
            if not isinstance(cd, dict):
                continue
            slim = {k: cd[k] for k in ("businessName", "business", "tags") if k in cd}
            if slim:
                cols[name] = slim
        out["columns"] = cols
        return out
    if kind == "classification_pii":
        cols = {}
        for name, cd in (delta.get("columns") or {}).items():
            if not isinstance(cd, dict):
                continue
            slim = {k: cd[k] for k in ("pii", "tags") if k in cd}
            if slim:
                cols[name] = slim
        out["columns"] = cols
        return out
    if kind == "table_definition":
        if delta.get("table"):
            out["table"] = delta["table"]
        if delta.get("table_tags") is not None:
            out["table_tags"] = delta["table_tags"]
        out["columns"] = {}
        return out
    if kind == "contract_review":
        cols = {}
        for name, cd in (delta.get("columns") or {}).items():
            if not isinstance(cd, dict):
                continue
            slim = {
                k: cd[k]
                for k in ("businessName", "business", "tags", "pii")
                if k in cd
            }
            if slim:
                cols[name] = slim
        if delta.get("table"):
            out["table"] = delta["table"]
        if delta.get("table_tags") is not None:
            out["table_tags"] = delta["table_tags"]
        out["columns"] = cols
        return out
    return dict(delta)


def extract_review_findings(raw: Any) -> list[dict[str, str]]:
    """Pull ``review.findings`` from an LLM payload (never written to the contract)."""
    if not isinstance(raw, dict):
        return []
    review = raw.get("review")
    if isinstance(review, dict):
        items = review.get("findings") or []
    elif isinstance(review, list):
        items = review
    else:
        return []
    findings: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        msg = str(item.get("message") or item.get("reason") or "").strip()
        if not msg:
            continue
        target = str(item.get("target") or item.get("column") or "table").strip() or "table"
        severity = str(item.get("severity") or "warning").strip().lower() or "warning"
        if severity not in ("info", "warning", "error"):
            severity = "warning"
        findings.append({
            "severity": severity,
            "target": target[:200],
            "message": msg[:500],
        })
    return findings


def count_delta_changes(delta: dict[str, Any]) -> int:
    """Count independently reviewable changes in a delta."""
    n = len(delta.get("columns") or {})
    if delta.get("table"):
        n += 1
    if delta.get("table_tags") is not None:
        n += 1
    return n


def cap_delta_changes(delta: dict[str, Any], max_changes: int) -> tuple[dict[str, Any], bool]:
    """Keep at most ``max_changes`` table/column edits. Returns (delta, truncated)."""
    if max_changes < 0:
        max_changes = 0
    total = count_delta_changes(delta)
    if total <= max_changes:
        return dict(delta), False
    out: dict[str, Any] = {"columns": {}}
    kept = 0
    if delta.get("table") and kept < max_changes:
        out["table"] = delta["table"]
        kept += 1
    if delta.get("table_tags") is not None and kept < max_changes:
        out["table_tags"] = delta["table_tags"]
        kept += 1
    for name in sorted((delta.get("columns") or {}).keys()):
        if kept >= max_changes:
            break
        cd = (delta.get("columns") or {}).get(name)
        if isinstance(cd, dict):
            out["columns"][name] = cd
            kept += 1
    return out, True


def assert_odcs_v3_contract(contract: dict) -> list[str]:
    """Return validation errors for a lifecycle contract artifact (ODCS v3.x)."""
    errors: list[str] = []
    if not isinstance(contract, dict):
        return ["contract must be a mapping"]

    forbidden = (
        "enrichment_meta",
        "provenance",
        "pii_summary",
        "last_updated",
        "last_updated_by_workflow",
        "_scan_metadata",
    )
    for key in forbidden:
        if key in contract:
            errors.append(f"telemetry key {key!r} must not appear in contract artifact")

    api = str(contract.get("apiVersion") or "")
    if not re.match(r"^v3\.\d+", api):
        errors.append(f"apiVersion must be ODCS v3.x, got {api!r}")

    try:
        from redibis.store.contract_store import ContractStore

        result = ContractStore.validate(contract, strict=False)
        errors.extend(result.get("errors") or [])
    except Exception as exc:
        errors.append(f"ODCS validate: {exc}")
    return errors
