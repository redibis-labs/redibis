"""
redibis.enrich.diff — structured diff between pre- and post-enrichment contracts.

Compares enrichment-relevant fields only (business layer, tags, PII review).
Quality rules and structural schema are unchanged by enrichment and are omitted.
"""

from __future__ import annotations

import copy
from typing import Any, Optional


def build_enrichment_diff(
    before: dict,
    after: dict,
    *,
    pii_demotions: Optional[list[str]] = None,
) -> dict:
    """
    Build a human-reviewable diff report for an enrichment run.

    ``before`` is the active contract at enrich time; ``after`` is the candidate.
    """
    before_schema = _first_schema(before)
    after_schema = _first_schema(after)
    before_cols = _columns_map(before)
    after_cols = _columns_map(after)
    all_names = sorted(set(before_cols) | set(after_cols))

    table_tags = _list_set_diff(
        before_schema.get("tags") if before_schema else [],
        after_schema.get("tags") if after_schema else [],
    )
    before_desc = (before_schema or {}).get("description") or before.get("description")
    after_desc = (after_schema or {}).get("description") or after.get("description")
    if isinstance(before_desc, dict):
        before_desc = before_desc.get("description") or before_desc.get("purpose")
    if isinstance(after_desc, dict):
        after_desc = after_desc.get("description") or after_desc.get("purpose")
    table_description = None
    if (before_desc or "") != (after_desc or ""):
        table_description = {"before": before_desc or "", "after": after_desc or ""}

    column_rows: list[dict] = []
    counts = {
        "columns_total": len(all_names),
        "columns_changed": 0,
        "definitions_added": 0,
        "definitions_updated": 0,
        "business_names_set": 0,
        "tag_changes": 0,
        "pii_changes": 0,
        "pii_demotions_pending": len(pii_demotions or []),
        "table_description_changed": 1 if table_description else 0,
    }

    for name in all_names:
        b_snap = _column_snapshot(before_cols.get(name, {}))
        a_snap = _column_snapshot(after_cols.get(name, {}))
        changes = _diff_column(b_snap, a_snap)
        if name in (pii_demotions or []):
            changes.append({
                "field": "pii_review",
                "kind": "demotion",
                "before": b_snap.get("classification") or "pii",
                "after": "not_pii (applied on merge)",
            })
            counts["pii_changes"] += 1
        if not changes:
            continue
        counts["columns_changed"] += 1
        for ch in changes:
            field = ch.get("field", "")
            if field == "business.definition":
                if not b_snap.get("business", {}).get("definition"):
                    counts["definitions_added"] += 1
                else:
                    counts["definitions_updated"] += 1
            elif field == "businessName":
                counts["business_names_set"] += 1
            elif field == "tags":
                counts["tag_changes"] += 1
            elif field.startswith("pii") or field == "classification" or field == "entity_type":
                counts["pii_changes"] += 1
        column_rows.append({"column": name, "changes": changes})

    return {
        "summary": counts,
        "table_tags": table_tags,
        "table_description": table_description,
        "columns": column_rows,
        "pii_demotions_pending": list(pii_demotions or []),
        "unchanged_columns": [
            n for n in all_names
            if n not in {r["column"] for r in column_rows}
        ],
    }


def _first_schema(contract: dict) -> dict:
    schema = contract.get("schema") or []
    return schema[0] if schema else {}


def _columns_map(contract: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for prop in _first_schema(contract).get("properties") or []:
        if isinstance(prop, dict) and prop.get("name"):
            out[prop["name"]] = prop
    return out


def _column_snapshot(prop: dict) -> dict:
    from redibis.contracts.privacy import col_pii_engine

    ce = col_pii_engine(prop) if prop else {}
    business = copy.deepcopy(prop.get("business")) if prop.get("business") else {}
    return {
        "businessName": prop.get("businessName"),
        "business": business,
        "tags": sorted(prop.get("tags") or []),
        "classification": prop.get("classification"),
        "entity_type": ce.get("entity_type"),
    }


def _list_set_diff(before: list, after: list) -> dict:
    b, a = set(before or []), set(after or [])
    return {
        "before": sorted(b),
        "after": sorted(a),
        "added": sorted(a - b),
        "removed": sorted(b - a),
    }


def _diff_column(before: dict, after: dict) -> list[dict]:
    changes: list[dict] = []

    if before.get("businessName") != after.get("businessName"):
        changes.append(_scalar_change(
            "businessName", before.get("businessName"), after.get("businessName"),
        ))

    b_biz = before.get("business") or {}
    a_biz = after.get("business") or {}
    if b_biz.get("definition") != a_biz.get("definition"):
        changes.append(_scalar_change(
            "business.definition", b_biz.get("definition"), a_biz.get("definition"),
        ))
    for key in ("synonyms", "example_values", "tags"):
        tag_diff = _list_set_diff(b_biz.get(key), a_biz.get(key))
        if tag_diff["added"] or tag_diff["removed"]:
            changes.append({"field": f"business.{key}", "kind": "list", **tag_diff})

    tag_diff = _list_set_diff(before.get("tags"), after.get("tags"))
    if tag_diff["added"] or tag_diff["removed"]:
        changes.append({"field": "tags", "kind": "list", **tag_diff})

    if before.get("classification") != after.get("classification"):
        changes.append(_scalar_change(
            "classification",
            before.get("classification"),
            after.get("classification"),
        ))

    if before.get("entity_type") != after.get("entity_type"):
        changes.append(_scalar_change(
            "entity_type",
            before.get("entity_type"),
            after.get("entity_type"),
        ))

    return changes


def _scalar_change(field: str, before: Any, after: Any) -> dict:
    return {
        "field": field,
        "kind": "scalar",
        "before": before,
        "after": after,
    }
