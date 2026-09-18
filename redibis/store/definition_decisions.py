"""
redibis.store.definition_decisions — steward overrides for definitions + tags.

Tags normally merge via set-union in ContractMerger. When a steward sets tags
through the Definitions view, this overlay stores the **authoritative** tag list
and reconcile replaces (not unions) on every upsert.
"""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from redibis.store.storage_backend import StorageBackend


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_active(entry: dict) -> bool:
    lifecycle = str((entry or {}).get("lifecycle_state") or "active").lower()
    return lifecycle not in ("stale", "superseded")


def reconcile_definition_decisions(contract: dict, decisions: dict) -> bool:
    """Apply definition/tag decisions in place. Returns True if changed."""
    if not decisions:
        return False
    changed = False
    table_dec = decisions.get("table") or {}
    col_decs = decisions.get("columns") or {}

    for schema_obj in contract.get("schema", []) or []:
        if _is_active(table_dec):
            if table_dec.get("tags") is not None:
                schema_obj["tags"] = list(table_dec["tags"])
                changed = True
            if table_dec.get("description") is not None:
                schema_obj["description"] = copy.deepcopy(table_dec["description"])
                changed = True
        for prop in schema_obj.get("properties", []) or []:
            if not isinstance(prop, dict):
                continue
            col = prop.get("name")
            cd = col_decs.get(col)
            if not cd or not _is_active(cd):
                continue
            if cd.get("tags") is not None:
                prop["tags"] = list(cd["tags"])
                changed = True
            for fld in ("description", "businessName", "business"):
                if fld in cd and cd[fld] is not None:
                    prop[fld] = copy.deepcopy(cd[fld])
                    changed = True
    return changed


@dataclass
class DefinitionPatch:
    """Partial definitions update for one table or column."""
    tags: Optional[list] = None
    description: Any = None
    businessName: Optional[str] = None
    business: Optional[dict] = None
    decided_by: str = ""
    ts: str = field(default_factory=_utc_now_iso)

    def to_dict(self) -> dict:
        d = asdict(self)
        return {k: v for k, v in d.items() if v is not None and k != "ts"}


class DefinitionDecisionStore:
    PREFIX = "_meta/definition_decisions"

    def __init__(self, backend: StorageBackend, bucket: str):
        self.backend = backend
        self.bucket = bucket

    def _key(self, table: str) -> str:
        return f"{self.PREFIX}/{table}.json"

    def get(self, table: str) -> dict:
        key = self._key(table)
        if not self.backend.exists(self.bucket, key):
            return {"table": {}, "columns": {}}
        raw = self.backend.get_json(self.bucket, key)
        return {
            "table": raw.get("table") or {},
            "columns": raw.get("columns") or {},
        }

    def patch_table(self, table: str, patch: dict, *, decided_by: str = "") -> dict:
        state = self.get(table)
        entry = dict(state.get("table") or {})
        entry.update(patch)
        entry["decided_by"] = decided_by
        entry["ts"] = _utc_now_iso()
        if "lifecycle_state" not in patch:
            entry["lifecycle_state"] = "active"
        if "decision_version" not in patch:
            entry["decision_version"] = int(entry.get("decision_version") or 0) + 1
        state["table"] = entry
        self._save(table, state)
        return entry

    def patch_column(self, table: str, column: str, patch: dict, *, decided_by: str = "") -> dict:
        state = self.get(table)
        cols = dict(state.get("columns") or {})
        entry = dict(cols.get(column) or {})
        entry.update(patch)
        entry["decided_by"] = decided_by
        entry["ts"] = _utc_now_iso()
        if "lifecycle_state" not in patch:
            entry["lifecycle_state"] = "active"
        if "decision_version" not in patch:
            entry["decision_version"] = int(entry.get("decision_version") or 0) + 1
        cols[column] = entry
        state["columns"] = cols
        self._save(table, state)
        return entry

    def mark_column_stale(self, table: str, column: str) -> bool:
        state = self.get(table)
        cols = dict(state.get("columns") or {})
        entry = cols.get(column)
        if not entry:
            return False
        if str(entry.get("lifecycle_state") or "active") == "stale":
            return False
        entry = dict(entry)
        entry["lifecycle_state"] = "stale"
        cols[column] = entry
        state["columns"] = cols
        self._save(table, state)
        return True

    def clear(self, table: str) -> None:
        key = self._key(table)
        if self.backend.exists(self.bucket, key):
            self.backend.delete(self.bucket, key)

    def _save(self, table: str, state: dict) -> None:
        self.backend.put_json(self.bucket, self._key(table),
                              {"table": table, **state})


def evaluate_and_mark_definition_drift(table: str, merged_contract: dict, store: "DefinitionDecisionStore") -> list[str]:
    """Demote fingerprint-mismatched *active* definition decisions to ``stale``."""
    from redibis.review.drift import LIFECYCLE_ACTIVE, LIFECYCLE_STALE, evaluate_column_drift
    from redibis.review.fingerprint import ColumnFingerprintSnapshot, fingerprint_from_contract_prop

    state = store.get(table)
    col_decs = state.get("columns") or {}
    if not col_decs:
        return []
    by_name = {}
    for schema_obj in merged_contract.get("schema", []) or []:
        for prop in schema_obj.get("properties", []) or []:
            if isinstance(prop, dict) and prop.get("name") is not None:
                by_name.setdefault(prop.get("name"), prop)
    stale_cols: list[str] = []
    for column, raw in col_decs.items():
        if str(raw.get("lifecycle_state") or LIFECYCLE_ACTIVE) != LIFECYCLE_ACTIVE:
            continue
        if not raw.get("fingerprint_key"):
            continue
        baseline = ColumnFingerprintSnapshot.from_dict({**raw, "column": column})
        prop = by_name.get(column)
        current = fingerprint_from_contract_prop(column, prop) if prop is not None else None
        drift = evaluate_column_drift(baseline, current)
        if drift.state == LIFECYCLE_STALE:
            store.mark_column_stale(table, column)
            stale_cols.append(column)
    return stale_cols
