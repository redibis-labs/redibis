"""
redibis.contracts.glossary — write approved business glossary into ODCS schema properties.
"""

from __future__ import annotations

from typing import Any, Optional


def apply_glossary_to_contract(
    partial: dict,
    column: str,
    glossary: Optional[str],
    *,
    business_name: Optional[str] = None,
) -> dict:
    """Set ``description`` (and optional ``businessName``) on a governed column.

    Respects invariant #14: only writes when a real glossary/definition exists.
    """
    if not glossary or not glossary.strip():
        return partial
    for schema_obj in partial.get("schema") or []:
        for prop in schema_obj.get("properties") or []:
            if isinstance(prop, dict) and prop.get("name") == column:
                prop["description"] = glossary.strip()
                if business_name:
                    prop["businessName"] = business_name
                return partial
    # Column not found — append a minimal property stub with glossary only
    schema_list = partial.setdefault("schema", [{"properties": []}])
    if not schema_list:
        schema_list.append({"properties": []})
    schema_list[0].setdefault("properties", []).append({
        "name": column,
        "logicalType": "string",
        "description": glossary.strip(),
        **({"businessName": business_name} if business_name else {}),
    })
    return partial
