"""Fingerprint-gated field decisions (definition / tags / classification / …).

PII decisions already re-apply on the next scan via ``PiiDecisionStore``. This
store extends that pattern to every other steward-reviewed field so a decision
remembered for a column re-applies only while the column fingerprint matches.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from redibis.review.drift import LIFECYCLE_ACTIVE, LIFECYCLE_STALE, evaluate_column_drift
from redibis.review.fingerprint import ColumnFingerprintSnapshot, fingerprint_from_contract_prop
from redibis.store.storage_backend import StorageBackend

VALID_FIELDS = frozenset({
    "classification",
    "tags",
    "definition",
    "logical_type",
    "masking",
    "quality_rules",
    "entity_type",
    "freshness",
    "retention",
    "cost",
})


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iter_columns(contract: dict):
    for schema_obj in contract.get("schema", []) or []:
        for prop in schema_obj.get("properties", []) or []:
            if isinstance(prop, dict):
                yield prop


@dataclass
class FieldDecision:
    column: str
    field: str
    value: Any = None
    fingerprint_key: str = ""
    name_normalized: str = ""
    logical_type: str = ""
    physical_type: str = ""
    format_signature: str = ""
    lifecycle_state: str = LIFECYCLE_ACTIVE
    decision_version: int = 1
    decided_by: str = ""
    reason: str = ""
    rationale_code: str = ""
    chosen_source: str = ""
    chosen_run_id: str = ""
    ts: str = field(default_factory=_utc_now_iso)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "FieldDecision":
        return cls(
            column=str(d.get("column") or ""),
            field=str(d.get("field") or ""),
            value=d.get("value"),
            fingerprint_key=str(d.get("fingerprint_key") or ""),
            name_normalized=str(d.get("name_normalized") or ""),
            logical_type=str(d.get("logical_type") or ""),
            physical_type=str(d.get("physical_type") or ""),
            format_signature=str(d.get("format_signature") or ""),
            lifecycle_state=str(d.get("lifecycle_state") or LIFECYCLE_ACTIVE),
            decision_version=int(d.get("decision_version") or 1),
            decided_by=str(d.get("decided_by") or ""),
            reason=str(d.get("reason") or ""),
            rationale_code=str(d.get("rationale_code") or ""),
            chosen_source=str(d.get("chosen_source") or ""),
            chosen_run_id=str(d.get("chosen_run_id") or ""),
            ts=str(d.get("ts") or _utc_now_iso()),
        )


class FieldDecisionStore:
    PREFIX = "_meta/field_decisions"

    def __init__(self, backend: StorageBackend, bucket: str):
        self.backend = backend
        self.bucket = bucket

    def _key(self, table: str) -> str:
        return f"{self.PREFIX}/{table}.json"

    def get(self, table: str) -> dict[str, dict[str, dict]]:
        """``{column: {field: decision_dict}}``."""
        key = self._key(table)
        if not self.backend.exists(self.bucket, key):
            return {}
        raw = self.backend.get_json(self.bucket, key) or {}
        return raw.get("columns") or {}

    def get_column(self, table: str, column: str) -> dict[str, dict]:
        return dict((self.get(table) or {}).get(column) or {})

    def set(self, table: str, decision: FieldDecision) -> dict:
        if decision.field not in VALID_FIELDS:
            raise ValueError(f"field must be one of {sorted(VALID_FIELDS)}")
        columns = self.get(table)
        col = dict(columns.get(decision.column) or {})
        col[decision.field] = decision.to_dict()
        columns[decision.column] = col
        self.backend.put_json(
            self.bucket, self._key(table),
            {"table": table, "columns": columns},
        )
        return decision.to_dict()

    def clear(self, table: str) -> None:
        key = self._key(table)
        if self.backend.exists(self.bucket, key):
            self.backend.delete(self.bucket, key)


def evaluate_and_mark_field_drift(
    table: str,
    merged_contract: dict,
    store: FieldDecisionStore,
) -> list[tuple[str, str]]:
    """Demote fingerprint-mismatched *active* field decisions to ``stale``.

    Returns ``[(column, field), ...]`` newly marked stale.
    """
    decisions = store.get(table)
    if not decisions:
        return []
    by_name = {p.get("name"): p for p in _iter_columns(merged_contract) if p.get("name") is not None}
    stale: list[tuple[str, str]] = []
    for column, fields in decisions.items():
        prop = by_name.get(column)
        current = fingerprint_from_contract_prop(column, prop) if prop is not None else None
        for field_name, raw in (fields or {}).items():
            if str(raw.get("lifecycle_state") or LIFECYCLE_ACTIVE) != LIFECYCLE_ACTIVE:
                continue
            if not raw.get("fingerprint_key"):
                continue
            baseline = ColumnFingerprintSnapshot.from_dict(raw)
            drift = evaluate_column_drift(baseline, current)
            if drift.state == LIFECYCLE_STALE:
                updated = FieldDecision.from_dict({**raw, "lifecycle_state": LIFECYCLE_STALE})
                store.set(table, updated)
                stale.append((column, field_name))
    return stale


def fingerprint_kwargs_from_prop(column: str, prop: Optional[dict]) -> dict[str, str]:
    fp = fingerprint_from_contract_prop(column, prop or {})
    return {
        "fingerprint_key": fp.fingerprint_key,
        "name_normalized": fp.name_normalized,
        "logical_type": fp.logical_type,
        "physical_type": fp.physical_type,
        "format_signature": fp.format_signature,
    }
