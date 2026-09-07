"""Read projections for the three scoped contract views (PII, quality, definitions)."""

from __future__ import annotations

from typing import Any, Optional

from redibis.contracts.privacy import (
    column_is_pii,
    col_entity_type,
    col_masking_policy,
    col_privacy_classification,
)
from redibis.contracts.rules import extract_rules


def _table_name(contract: dict) -> str:
    db = contract.get("database_name") or ""
    tbl = contract.get("table_name") or ""
    return f"{db}.{tbl}" if db else tbl


def build_pii_view(contract: dict, pii_decisions: dict) -> dict:
    """PII columns only + masking policy + decision overlay."""
    rows = []
    for schema_obj in contract.get("schema", []) or []:
        for prop in schema_obj.get("properties", []) or []:
            if not isinstance(prop, dict):
                continue
            col = prop.get("name")
            dec = pii_decisions.get(col) or {}
            if dec.get("status") == "not_pii":
                continue
            if not column_is_pii(prop):
                continue
            is_pii = True
            privacy = prop.get("privacy") or {}
            mp = col_masking_policy(prop)
            rows.append({
                "column": col,
                "classification": prop.get("classification") or col_privacy_classification(prop),
                "entity_type": col_entity_type(prop),
                "tags": prop.get("tags") or [],
                "privacy": privacy or None,
                "masking_policy": mp or None,
                "decision": dec or None,
                "is_pii": is_pii and dec.get("status") != "not_pii",
            })
    return {"table": _table_name(contract), "columns": rows, "count": len(rows)}


def build_quality_view(contract: dict, quality_decisions: dict) -> dict:
    """Active rules + suppressed/manual decisions."""
    rules = [r.to_dict() for r in extract_rules(contract)]
    active_ids = {r["rule_id"] for r in rules}
    suppressed = []
    manual = []
    for rid, d in quality_decisions.items():
        st = (d or {}).get("status")
        if st == "suppressed":
            suppressed.append({"rule_id": rid, **d})
        elif st == "manual" and rid not in active_ids:
            manual.append({"rule_id": rid, **d})
    return {
        "table": _table_name(contract),
        "rules": rules,
        "suppressed": suppressed,
        "pending_manual": manual,
        "counts": {
            "active": len(rules),
            "suppressed": len(suppressed),
        },
    }


def build_definitions_view(contract: dict, definition_decisions: dict) -> dict:
    """Table + column business metadata only."""
    schema = (contract.get("schema") or [{}])[0]
    table_block = {
        "description": schema.get("description"),
        "tags": schema.get("tags") or [],
        "decision": definition_decisions.get("table") or {},
    }
    columns = []
    col_decs = definition_decisions.get("columns") or {}
    for prop in schema.get("properties", []) or []:
        if not isinstance(prop, dict):
            continue
        col = prop.get("name")
        b = prop.get("business") or {}
        if isinstance(b, dict) and b.get("definition") in ("test1", "test"):
            b = {**b, "_stub": True}
        columns.append({
            "column": col,
            "businessName": prop.get("businessName"),
            "description": prop.get("description"),
            "business": b,
            "tags": prop.get("tags") or [],
            "decision": col_decs.get(col) or {},
        })
    return {
        "table": _table_name(contract),
        "table_definition": table_block,
        "columns": columns,
    }
