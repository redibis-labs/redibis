"""
redibis.store.quality_decisions — quality rule decision overlay.

The merger unions and never deletes quality rules from a re-scan. This overlay
records steward intent:

  - ``suppressed`` — rule must not appear in the contract (overlay wins on merge)
  - ``manual``     — rule payload is authoritative (add or replace by rule_id)

Edit flow for profiler rules: suppress original rule_id + add new ``manual`` rule.
"""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from redibis.store.storage_backend import StorageBackend

VALID_STATUSES = frozenset({"suppressed", "manual", "active"})


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iter_quality_entries(contract: dict):
    """Yield (column_name|None, index, quality_dict, parent_list, schema_obj)."""
    for schema_obj in contract.get("schema", []) or []:
        tq = schema_obj.get("quality")
        if isinstance(tq, list):
            for i, q in enumerate(tq):
                if isinstance(q, dict):
                    yield None, i, q, tq, schema_obj
        for prop in schema_obj.get("properties", []) or []:
            if not isinstance(prop, dict):
                continue
            col = prop.get("name")
            cq = prop.get("quality")
            if isinstance(cq, list):
                for i, q in enumerate(cq):
                    if isinstance(q, dict):
                        yield col, i, q, cq, prop


def reconcile_quality_rules(contract: dict, decisions: dict[str, dict]) -> bool:
    """Apply quality decisions to ``contract`` in place. Returns True if changed."""
    from redibis.contracts.rules import stable_rule_id

    if not decisions:
        return False

    changed = False
    suppressed = {rid for rid, d in decisions.items()
                  if (d or {}).get("status") == "suppressed"}

    # Remove suppressed rules (iterate backwards per list)
    by_list: dict[int, list[tuple]] = {}
    for col, idx, q, lst, parent in _iter_quality_entries(contract):
        rid = stable_rule_id(col, q)
        by_list.setdefault(id(lst), []).append((col, idx, q, lst, parent, rid))

    for lst_id, entries in by_list.items():
        lst = entries[0][3]
        for _col, idx, _q, _lst, _parent, rid in sorted(entries, key=lambda x: x[1], reverse=True):
            if rid in suppressed:
                lst.pop(idx)
                changed = True

    # Apply manual rules
    for rid, decision in decisions.items():
        if (decision or {}).get("status") != "manual":
            continue
        payload = copy.deepcopy((decision or {}).get("payload") or {})
        if not payload:
            continue
        meta = dict(payload.get("meta") or {})
        meta["redibis_rule_id"] = rid
        payload["meta"] = meta
        col = decision.get("column")
        placed = False
        for schema_obj in contract.get("schema", []) or []:
            if col is None:
                ql = schema_obj.setdefault("quality", [])
                _replace_or_append(ql, rid, payload, None)
                placed = True
                changed = True
                break
            for prop in schema_obj.get("properties", []) or []:
                if prop.get("name") == col:
                    ql = prop.setdefault("quality", [])
                    _replace_or_append(ql, rid, payload, col)
                    placed = True
                    changed = True
                    break
            if placed:
                break
    return changed


def effective_contract_quality(contract: dict, decisions: Optional[dict[str, dict]]) -> dict:
    """Return a copy of ``contract`` with the quality-decision overlay applied.

    Used everywhere a rule set is *executed* or *exported* (monitor runs, CLI
    package export, web package export) so suppressed rules never run and
    approved manual rules always do — mirroring exactly what
    ``ContractStore.upsert()`` does at write time, without persisting anything.
    """
    if not decisions:
        return contract
    working = copy.deepcopy(contract)
    reconcile_quality_rules(working, decisions)
    return working


def _replace_or_append(ql: list, rule_id: str, payload: dict, column: Optional[str]) -> None:
    from redibis.contracts.rules import stable_rule_id
    for i, existing in enumerate(ql):
        if not isinstance(existing, dict):
            continue
        if stable_rule_id(column, existing) == rule_id:
            ql[i] = payload
            return
        meta = existing.get("meta") or {}
        if meta.get("redibis_rule_id") == rule_id:
            ql[i] = payload
            return
    ql.append(payload)


@dataclass
class QualityDecision:
    rule_id: str
    status: str
    column: Optional[str] = None
    payload: dict = field(default_factory=dict)
    source_rule_id: Optional[str] = None
    decided_by: str = ""
    run_id: str = ""
    ts: str = field(default_factory=_utc_now_iso)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "QualityDecision":
        return cls(
            rule_id=d["rule_id"],
            status=d.get("status", "suppressed"),
            column=d.get("column"),
            payload=d.get("payload") or {},
            source_rule_id=d.get("source_rule_id"),
            decided_by=d.get("decided_by", ""),
            run_id=d.get("run_id", ""),
            ts=d.get("ts") or _utc_now_iso(),
        )


class QualityDecisionStore:
    PREFIX = "_meta/quality_decisions"

    def __init__(self, backend: StorageBackend, bucket: str):
        self.backend = backend
        self.bucket = bucket

    def _key(self, table: str) -> str:
        return f"{self.PREFIX}/{table}.json"

    def get(self, table: str) -> dict[str, dict]:
        key = self._key(table)
        if not self.backend.exists(self.bucket, key):
            return {}
        raw = self.backend.get_json(self.bucket, key)
        return raw.get("decisions", {}) or {}

    def list(self, table: str) -> list[dict]:
        items = list(self.get(table).values())
        items.sort(key=lambda d: d.get("ts", ""), reverse=True)
        return items

    def set(self, table: str, decision: QualityDecision) -> dict:
        if decision.status not in VALID_STATUSES:
            raise ValueError(f"status must be one of {sorted(VALID_STATUSES)}")
        decisions = self.get(table)
        decisions[decision.rule_id] = decision.to_dict()
        self._save(table, decisions)
        return decision.to_dict()

    def remove(self, table: str, rule_id: str) -> bool:
        decisions = self.get(table)
        if rule_id in decisions:
            del decisions[rule_id]
            self._save(table, decisions)
            return True
        return False

    def clear(self, table: str) -> None:
        key = self._key(table)
        if self.backend.exists(self.bucket, key):
            self.backend.delete(self.bucket, key)

    def _save(self, table: str, decisions: dict) -> None:
        self.backend.put_json(self.bucket, self._key(table),
                              {"table": table, "decisions": decisions})
