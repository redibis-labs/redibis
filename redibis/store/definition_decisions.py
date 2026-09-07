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


def reconcile_definition_decisions(contract: dict, decisions: dict) -> bool:
    """Apply definition/tag decisions in place. Returns True if changed."""
    if not decisions:
        return False
    changed = False
    table_dec = decisions.get("table") or {}
    col_decs = decisions.get("columns") or {}

    for schema_obj in contract.get("schema", []) or []:
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
            if not cd:
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
        cols[column] = entry
        state["columns"] = cols
        self._save(table, state)
        return entry

    def clear(self, table: str) -> None:
        key = self._key(table)
        if self.backend.exists(self.bucket, key):
            self.backend.delete(self.bucket, key)

    def _save(self, table: str, state: dict) -> None:
        self.backend.put_json(self.bucket, self._key(table),
                              {"table": table, **state})
