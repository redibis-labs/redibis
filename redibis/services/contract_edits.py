"""Column edits to the live contract, routed to the decision overlay that owns each field.

The active contract is one object built from the scan merges plus the steward overlays
(``ContractStore``: PII decisions, quality decisions, definition decisions). The overlays
are re-applied on every upsert, so an edit written straight into the contract body is
undone by the next write when an overlay owns that field — the edit must go to the
overlay. Every page that edits a contract (Contract quick edit, Steward Review, share
links) goes through the same overlays, so they all see and keep each other's changes.

========================  =========================================================
field                     written through
========================  =========================================================
description, business,    ``ContractStore.patch_definitions`` (definition overlay)
businessName, tags
PII on / off              ``ContractStore.set_pii_decision`` (PII overlay)
classification, privacy,  ``set_pii_decision("pii", payload)`` — keeps the column's
pii, maskingPolicy        masking and entity type
quality rule on / off     ``suppress_quality_rule`` / ``restore_quality_rule``
anything else             ``ContractStore.upsert`` of the edited contract
========================  =========================================================
"""

from __future__ import annotations

import copy
from typing import Any, Optional

DEFINITION_FIELDS = ("description", "business", "businessName", "tags")
PII_FIELDS = ("classification", "pii", "privacy", "maskingPolicy")

__all__ = ["DEFINITION_FIELDS", "PII_FIELDS", "split_column_edits", "apply_column_edits", "pii_switch"]


def split_column_edits(columns: dict[str, dict], *, active: dict,
                       allowed=lambda field: True) -> tuple[dict, dict, dict, list[str]]:
    """Split ``{column: {field: value}}`` into definition patches, PII field edits and the rest.

    ``allowed(field)`` filters by the caller's permission (e.g. a share link's scope).
    Tags are added to the column's current tags (as share links always did).
    Returns ``(definitions, pii, rest, applied_labels)``.
    """
    current = {p.get("name"): p for s in active.get("schema", []) or []
               for p in s.get("properties", []) or [] if isinstance(p, dict)}
    definitions: dict[str, dict] = {}
    pii: dict[str, dict] = {}
    rest: dict[str, dict] = {}
    applied: list[str] = []
    for column, fields in (columns or {}).items():
        if column not in current or not isinstance(fields, dict):
            continue
        for field, value in fields.items():
            if not allowed(field):
                continue
            if field == "tags":
                value = sorted(set(current[column].get("tags") or []) | set(value or []))
            if field in DEFINITION_FIELDS:
                definitions.setdefault(column, {})[field] = value
            elif field in PII_FIELDS:
                pii.setdefault(column, {})[field] = value
            else:
                rest.setdefault(column, {})[field] = value
            applied.append(f"{column}.{field}")
    return definitions, pii, rest, applied


def apply_column_edits(store: Any, table: str, columns: dict[str, dict], *, decided_by: str,
                       run_id: str = "", allowed=lambda field: True,
                       validate: bool = False) -> dict:
    """Apply column edits through their overlays; returns ``{"applied": [...], "version_after": …}``."""
    active = store.get_active(table)
    if active is None:
        raise ValueError(f"No active contract for {table!r}")
    definitions, pii, rest, applied = split_column_edits(columns, active=active, allowed=allowed)
    for column in list(pii):
        # A plain classification ("internal") of a column that is not PII stays a plain field.
        fields = pii[column]
        if set(fields) == {"classification"} and not _is_pii_now(store, table, active, column) \
                and not str(fields["classification"] or "").lower().startswith("pii"):
            rest.setdefault(column, {}).update(pii.pop(column))
    if definitions:
        store.patch_definitions(table, column_patches=definitions, decided_by=decided_by,
                                run_id=run_id or "definitions-patch")
    for column, fields in pii.items():
        _apply_pii_fields(store, table, column, fields, decided_by=decided_by, run_id=run_id)
    if rest:
        edited = copy.deepcopy(store.get_active(table))
        for schema_obj in edited.get("schema", []) or []:
            for prop in schema_obj.get("properties", []) or []:
                prop.update(rest.get(prop.get("name")) or {})
        store.upsert(partial=edited, table=table, workflow="column-edit",
                     run_id=run_id or "column-edit", validate=validate)
    return {"applied": applied, "version_after": (store.get_active(table) or {}).get("version")}


def _is_pii_now(store: Any, table: str, active: dict, column: str) -> bool:
    """The steward decision wins over the column's own signals (as on Steward Review)."""
    from redibis.contracts.privacy import column_is_pii

    status = ((store.get_pii_decisions(table) or {}).get(column) or {}).get("status")
    if status in ("pii", "not_pii"):
        return status == "pii"
    return any(column_is_pii(p) for s in active.get("schema", []) or []
               for p in s.get("properties", []) or [] if isinstance(p, dict) and p.get("name") == column)


def _apply_pii_fields(store: Any, table: str, column: str, fields: dict, *,
                      decided_by: str, run_id: str) -> None:
    """Classification / privacy / masking edits on a column: it is (or becomes) PII."""
    from redibis.services.review_service import pii_on_payload

    pii_block = fields.get("pii") if isinstance(fields.get("pii"), dict) else {}
    if pii_block.get("detected") is False:
        store.set_pii_decision(table, column, "not_pii", decided_by=decided_by, run_id=run_id)
        return
    entity = pii_block.get("entity_type") or None
    payload = pii_on_payload(store, table, column, entity)
    if isinstance(fields.get("privacy"), dict):
        payload["privacy"] = copy.deepcopy(fields["privacy"])
    if isinstance(fields.get("maskingPolicy"), dict):
        privacy = payload.setdefault("privacy", {})
        mp = fields["maskingPolicy"]
        privacy["masking_policy"] = {
            "default_strategy": mp.get("default_strategy") or mp.get("default") or "mask",
            "reversible": bool(mp.get("reversible", False)),
            "role_overrides": mp.get("role_overrides") or mp.get("roles") or {},
        }
    if fields.get("classification") is not None:
        payload["classification"] = fields["classification"]
        if isinstance(payload.get("privacy"), dict):
            payload["privacy"]["classification"] = fields["classification"]
    store.set_pii_decision(table, column, "pii", entity_type=payload.get("entity_type"),
                           payload=payload, decided_by=decided_by,
                           run_id=run_id or f"column-edit:{column}")


def pii_switch(store: Any, table: str, column: str, on: bool, *, decided_by: str,
               entity_type: Optional[str] = None, run_id: str = "") -> Any:
    """Turn PII on (with its masking) or off for one column — the one switch every page uses."""
    if not on:
        return store.set_pii_decision(table, column, "not_pii", decided_by=decided_by, run_id=run_id)
    from redibis.services.review_service import pii_on_payload

    payload = pii_on_payload(store, table, column, entity_type)
    return store.set_pii_decision(table, column, "pii", entity_type=payload.get("entity_type"),
                                  payload=payload, decided_by=decided_by, run_id=run_id)
