"""Path-level diff between an active ODCS contract and a Deep Enrich candidate.

Diff items are keyed by dotted contract paths so stewards can accept/reject
individual facets (columns, SLA entries, customProperties) without merging
the whole candidate.
"""

from __future__ import annotations

import copy
from typing import Any, Optional


def _norm(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _norm(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, list):
        return [_norm(v) for v in value]
    return value


def _eq(a: Any, b: Any) -> bool:
    return _norm(a) == _norm(b)


def _props_by_name(contract: Optional[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not isinstance(contract, dict):
        return out
    for schema_obj in contract.get("schema") or []:
        if not isinstance(schema_obj, dict):
            continue
        for prop in schema_obj.get("properties") or []:
            if isinstance(prop, dict) and prop.get("name"):
                out[str(prop["name"])] = prop
    return out


def _sla_by_property(contract: Optional[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not isinstance(contract, dict):
        return out
    for entry in contract.get("slaProperties") or []:
        if isinstance(entry, dict) and entry.get("property"):
            out[str(entry["property"])] = entry
    return out


def _cps_by_property(contract: Optional[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not isinstance(contract, dict):
        return out
    for entry in contract.get("customProperties") or []:
        if isinstance(entry, dict) and entry.get("property"):
            out[str(entry["property"])] = entry
    return out


def _column_field_paths(prop: dict) -> dict[str, Any]:
    """Map steward-facing field names → values for one column property."""
    business = prop.get("business") if isinstance(prop.get("business"), dict) else {}
    privacy = prop.get("privacy") if isinstance(prop.get("privacy"), dict) else {}
    pii = prop.get("pii") if isinstance(prop.get("pii"), dict) else {}
    pii_priv = privacy.get("pii") if isinstance(privacy.get("pii"), dict) else {}

    definition = business.get("definition") or prop.get("description") or ""
    if isinstance(definition, dict):
        definition = definition.get("definition") or definition.get("purpose") or ""

    classification = str(prop.get("classification") or privacy.get("classification") or "")
    entity = prop.get("entity_type") or pii_priv.get("entity_type") or pii.get("entity_type")
    is_pii = None
    try:
        from redibis.contracts.privacy import column_is_pii
        is_pii = bool(column_is_pii(prop))
    except Exception:
        is_pii = bool(entity) or classification.lower().startswith("pii")

    return {
        "definition": str(definition).strip() if definition else "",
        "tags": list(prop.get("tags") or []),
        "classification": classification,
        "entity_type": entity or "",
        "logical_type": str(prop.get("logicalType") or prop.get("physicalType") or ""),
        "pii": {"is_pii": is_pii, "entity_type": entity or ""},
        "quality_rules": list(prop.get("quality") or []),
        "masking": privacy.get("masking_policy") or prop.get("maskingPolicy") or {},
    }


def _scope_for_path(path: str) -> str:
    if path.startswith("schema.properties."):
        return "column"
    if path.startswith("slaProperties."):
        return "sla"
    if path.startswith("customProperties."):
        return "custom"
    return "table"


def _classify_sla(prop_name: str) -> str:
    """Map SLA property names to steward facet ids."""
    name = (prop_name or "").lower()
    if name in ("retention", "freshness", "frequency", "latency", "timeofavailability"):
        if name == "timeofavailability":
            return "freshness"
        if name == "frequency":
            return "freshness"
        return name
    return prop_name


def _classify_custom(prop_name: str) -> str:
    name = (prop_name or "").lower()
    if name in ("cost", "business_cost", "businesscost", "data_product_cost"):
        return "cost"
    return prop_name


def diff_contracts(
    active: Optional[dict],
    candidate: Optional[dict],
) -> list[dict[str, Any]]:
    """Return path-level diff items between ``active`` and ``candidate``.

    Each item::

        {
          "path": "schema.properties.email.definition",
          "scope": "column" | "table" | "sla" | "custom",
          "column": "email" | None,
          "field": "definition" | "freshness" | "cost" | …,
          "before": <active value>,
          "after": <candidate value>,
          "kind": "add" | "change" | "remove",
        }
    """
    active = active or {}
    candidate = candidate or {}
    items: list[dict[str, Any]] = []

    # Table-level scalars
    for key, field in (
        ("name", "name"),
        ("description", "description"),
        ("owner", "owner"),
        ("businessName", "name"),
    ):
        before = active.get(key)
        after = candidate.get(key)
        if before is None and after is None:
            continue
        if _eq(before, after):
            continue
        # Prefer schema[0] businessName over top-level name when both exist —
        # only emit top-level when schema-level isn't the source of truth later.
        if key == "name" and (active.get("businessName") or candidate.get("businessName")):
            continue
        kind = "add" if before in (None, "", []) and after not in (None, "", []) else (
            "remove" if after in (None, "", []) else "change"
        )
        items.append({
            "path": key,
            "scope": "table",
            "column": None,
            "field": field,
            "before": before,
            "after": after,
            "kind": kind,
        })

    # Schema-level description / businessName / owner
    a_schema = (active.get("schema") or [{}])
    c_schema = (candidate.get("schema") or [{}])
    a0 = a_schema[0] if a_schema and isinstance(a_schema[0], dict) else {}
    c0 = c_schema[0] if c_schema and isinstance(c_schema[0], dict) else {}
    for key, field in (
        ("description", "description"),
        ("businessName", "name"),
        ("owner", "owner"),
    ):
        before = a0.get(key)
        after = c0.get(key)
        if _eq(before, after):
            continue
        if before in (None, "", []) and after in (None, "", []):
            continue
        kind = "add" if before in (None, "", []) else ("remove" if after in (None, "", []) else "change")
        items.append({
            "path": f"schema.{key}",
            "scope": "table",
            "column": None,
            "field": field,
            "before": before,
            "after": after,
            "kind": kind,
        })

    # Columns
    a_props = _props_by_name(active)
    c_props = _props_by_name(candidate)
    for col in sorted(set(a_props) | set(c_props)):
        a_fields = _column_field_paths(a_props.get(col) or {})
        c_fields = _column_field_paths(c_props.get(col) or {})
        for field in sorted(set(a_fields) | set(c_fields)):
            before = a_fields.get(field)
            after = c_fields.get(field)
            if _eq(before, after):
                continue
            if before in (None, "", [], {}) and after in (None, "", [], {}):
                continue
            kind = "add" if before in (None, "", [], {}) else (
                "remove" if after in (None, "", [], {}) else "change"
            )
            items.append({
                "path": f"schema.properties.{col}.{field}",
                "scope": "column",
                "column": col,
                "field": field,
                "before": before,
                "after": after,
                "kind": kind,
            })

    # SLA properties (freshness / retention / …)
    a_sla = _sla_by_property(active)
    c_sla = _sla_by_property(candidate)
    for prop_name in sorted(set(a_sla) | set(c_sla)):
        before = a_sla.get(prop_name)
        after = c_sla.get(prop_name)
        if _eq(before, after):
            continue
        kind = "add" if before is None else ("remove" if after is None else "change")
        facet = _classify_sla(prop_name)
        items.append({
            "path": f"slaProperties.{prop_name}",
            "scope": "sla",
            "column": None,
            "field": facet,
            "sla_property": prop_name,
            "before": before,
            "after": after,
            "kind": kind,
        })

    # customProperties (business cost, …) — skip operational pii_summary
    a_cp = {k: v for k, v in _cps_by_property(active).items() if k != "pii_summary"}
    c_cp = {k: v for k, v in _cps_by_property(candidate).items() if k != "pii_summary"}
    for prop_name in sorted(set(a_cp) | set(c_cp)):
        before = a_cp.get(prop_name)
        after = c_cp.get(prop_name)
        if _eq(before, after):
            continue
        kind = "add" if before is None else ("remove" if after is None else "change")
        facet = _classify_custom(prop_name)
        items.append({
            "path": f"customProperties.{prop_name}",
            "scope": "custom",
            "column": None,
            "field": facet,
            "custom_property": prop_name,
            "before": before,
            "after": after,
            "kind": kind,
        })

    return items


def summarize_diff(items: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {"add": 0, "change": 0, "remove": 0}
    by_scope: dict[str, int] = {}
    for it in items:
        kind = str(it.get("kind") or "change")
        counts[kind] = counts.get(kind, 0) + 1
        scope = str(it.get("scope") or "table")
        by_scope[scope] = by_scope.get(scope, 0) + 1
    return {
        "total": len(items),
        "kinds": counts,
        "by_scope": by_scope,
    }


def get_value_at_path(contract: Optional[dict], path: str) -> Any:
    """Read a steward path from a contract (best-effort)."""
    contract = contract or {}
    if path in ("name", "description", "owner", "businessName"):
        return contract.get(path)
    if path.startswith("schema.") and path.count(".") == 1:
        key = path.split(".", 1)[1]
        schema = (contract.get("schema") or [{}])
        s0 = schema[0] if schema and isinstance(schema[0], dict) else {}
        return s0.get(key)
    if path.startswith("schema.properties."):
        rest = path[len("schema.properties."):]
        col, _, field = rest.partition(".")
        prop = _props_by_name(contract).get(col) or {}
        return _column_field_paths(prop).get(field)
    if path.startswith("slaProperties."):
        prop_name = path[len("slaProperties."):]
        return _sla_by_property(contract).get(prop_name)
    if path.startswith("customProperties."):
        prop_name = path[len("customProperties."):]
        return _cps_by_property(contract).get(prop_name)
    return None


def set_value_on_partial(
    partial: dict,
    path: str,
    value: Any,
    *,
    active: Optional[dict] = None,
) -> None:
    """Apply one accepted path onto an ODCS partial (mutates ``partial``)."""
    active = active or {}
    db = partial.get("database_name") or active.get("database_name") or ""
    tbl = partial.get("table_name") or active.get("table_name") or ""

    if path in ("name", "businessName") or path == "schema.businessName":
        partial["name"] = value
        schema = partial.setdefault("schema", [{"name": f"{db}_{tbl}" if db else tbl, "properties": []}])
        if schema and isinstance(schema[0], dict):
            schema[0]["businessName"] = value
        return
    if path in ("description", "schema.description"):
        if path.startswith("schema."):
            schema = partial.setdefault("schema", [{"name": f"{db}_{tbl}" if db else tbl, "properties": []}])
            if schema and isinstance(schema[0], dict):
                schema[0]["description"] = value
        else:
            partial["description"] = value
        return
    if path in ("owner", "schema.owner"):
        partial["owner"] = value
        return

    if path.startswith("slaProperties."):
        prop_name = path[len("slaProperties."):]
        sla = list(partial.get("slaProperties") or [])
        # Start from active SLA so last-writer replace keeps unrelated entries.
        if "slaProperties" not in partial:
            sla = copy.deepcopy(list(active.get("slaProperties") or []))
        sla = [e for e in sla if not (isinstance(e, dict) and e.get("property") == prop_name)]
        if value is not None:
            if isinstance(value, dict):
                entry = dict(value)
                entry.setdefault("property", prop_name)
            else:
                entry = {"property": prop_name, "value": value}
            sla.append(entry)
        partial["slaProperties"] = sla
        return

    if path.startswith("customProperties."):
        prop_name = path[len("customProperties."):]
        cps = list(partial.get("customProperties") or [])
        if "customProperties" not in partial:
            cps = copy.deepcopy([
                e for e in (active.get("customProperties") or [])
                if isinstance(e, dict) and e.get("property") != "pii_summary"
            ])
        cps = [e for e in cps if not (isinstance(e, dict) and e.get("property") == prop_name)]
        if value is not None:
            if isinstance(value, dict):
                entry = dict(value)
                entry.setdefault("property", prop_name)
            else:
                entry = {"property": prop_name, "value": value}
            cps.append(entry)
        partial["customProperties"] = cps
        return

    if path.startswith("schema.properties."):
        rest = path[len("schema.properties."):]
        col, _, field = rest.partition(".")
        schema = partial.setdefault(
            "schema",
            [{"name": f"{db}_{tbl}" if db else tbl, "properties": []}],
        )
        if not schema or not isinstance(schema[0], dict):
            schema[:] = [{"name": f"{db}_{tbl}" if db else tbl, "properties": []}]
        props = schema[0].setdefault("properties", [])
        prop = None
        for p in props:
            if isinstance(p, dict) and p.get("name") == col:
                prop = p
                break
        if prop is None:
            # Seed from active column if present so we don't drop identity fields.
            seed = copy.deepcopy(_props_by_name(active).get(col) or {"name": col})
            seed["name"] = col
            props.append(seed)
            prop = seed

        if field == "definition":
            prop["description"] = value
            biz = prop.get("business") if isinstance(prop.get("business"), dict) else {}
            biz = dict(biz)
            biz["definition"] = value
            prop["business"] = biz
        elif field == "tags":
            prop["tags"] = list(value) if isinstance(value, list) else ([value] if value else [])
        elif field == "classification":
            prop["classification"] = value
            privacy = prop.get("privacy") if isinstance(prop.get("privacy"), dict) else {}
            privacy = dict(privacy)
            privacy["classification"] = value
            prop["privacy"] = privacy
        elif field == "entity_type":
            prop["entity_type"] = value
        elif field == "logical_type":
            prop["logicalType"] = value
        elif field == "quality_rules":
            prop["quality"] = list(value) if isinstance(value, list) else []
        elif field == "masking":
            if isinstance(value, dict):
                prop["maskingPolicy"] = value
        elif field == "pii":
            # PII is applied via overlay, not raw merge — still stamp payload for preview.
            if isinstance(value, dict):
                is_pii = bool(value.get("is_pii"))
                entity = value.get("entity_type")
                if is_pii:
                    prop["pii"] = {"detected": True, "entity_type": entity}
                    tags = set(prop.get("tags") or [])
                    tags.update({"pii", "gdpr_personal_data"})
                    prop["tags"] = sorted(tags)
                else:
                    prop.pop("pii", None)
        return

    # Unknown path — store under customProperties as a fallback marker
    cps = list(partial.get("customProperties") or [])
    cps.append({"property": f"deep_enrich_path:{path}", "value": value})
    partial["customProperties"] = cps


__all__ = [
    "diff_contracts",
    "summarize_diff",
    "get_value_at_path",
    "set_value_on_partial",
    "_scope_for_path",
]
