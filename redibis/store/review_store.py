"""
redibis.store.review_store
==========================
Final-Review checkpoint overlay — the resumable record of which columns of a contract a
human has approved (or edited/rejected), stored OUTSIDE the contract document under the
``_meta/`` namespace (same pattern as PiiDecisionStore / DefinitionDecisionStore).

Storage layout (contracts bucket, i.e. MinIO/S3 or local):
    _meta/reviews/{table}.json

The contract YAML itself is never modified by approval — only edits go through
``ContractStore.upsert``; approval is logged here. ``fully_approved`` flips true when every
column in the contract is approved (or via explicit finalize). Reopening a contract resumes
from this overlay.

Additive steward-review fields (backward compatible):
  - statuses ``needs_review`` and ``no_action``
  - per-field ``FieldVerdict`` on columns and table-level items
  - ``guaranteed`` alongside ``fully_approved``
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from redibis.store.storage_backend import StorageBackend

# Original four statuses remain valid; needs_review / no_action are additive.
VALID_STATUSES = {"pending", "approved", "edited", "rejected", "needs_review", "no_action"}
LEGACY_STATUSES = {"pending", "approved", "edited", "rejected"}

#: Column / table-item statuses that count as "this steward finished this item".
REVIEWED_STATUSES = frozenset({"approved", "edited", "no_action"})
#: Statuses that block the guaranteed stamp.
BLOCKING_STATUSES = frozenset({"pending", "rejected", "needs_review"})

DECISION_TO_STATUS = {
    "accept": "approved",
    "edit": "edited",
    "reject": "rejected",
    "needs_review": "needs_review",
    "no_action": "no_action",
}
STATUS_TO_DECISION = {v: k for k, v in DECISION_TO_STATUS.items()}
VALID_DECISIONS = frozenset(DECISION_TO_STATUS)
REVIEWED_DECISIONS = frozenset({"accept", "edit", "no_action"})
BLOCKING_DECISIONS = frozenset({"reject", "needs_review"})


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _scrub_text(text: str) -> str:
    if not text:
        return text
    from redibis.contracts.privacy import scrub_pii_text
    return scrub_pii_text(str(text))


def _scrub_field_verdict(verdict: "FieldVerdict") -> None:
    if verdict.rationale_text:
        verdict.rationale_text = _scrub_text(verdict.rationale_text)


def _scrub_column_review(review: "ColumnReview") -> None:
    if review.note:
        review.note = _scrub_text(review.note)
    for fv in (review.verdicts or {}).values():
        if isinstance(fv, FieldVerdict):
            _scrub_field_verdict(fv)
        elif isinstance(fv, dict) and fv.get("rationale_text"):
            fv["rationale_text"] = _scrub_text(fv["rationale_text"])


@dataclass
class FieldVerdict:
    """Structured steward decision for one field (or table-level item)."""
    field: str
    decision: str = "accept"  # accept | reject | needs_review | no_action | edit
    chosen_source: str = ""
    chosen_run_id: str = ""
    value: Any = None
    rationale_code: str = ""
    rationale_text: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    by: str = ""
    at: str = ""

    def to_dict(self) -> dict:
        return {
            "field": self.field,
            "decision": self.decision,
            "chosen_source": self.chosen_source,
            "chosen_run_id": self.chosen_run_id,
            "value": self.value,
            "rationale_code": self.rationale_code,
            "rationale_text": self.rationale_text,
            "evidence_refs": list(self.evidence_refs),
            "by": self.by,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "FieldVerdict":
        return cls(
            field=str(d.get("field") or ""),
            decision=str(d.get("decision") or "accept"),
            chosen_source=str(d.get("chosen_source") or ""),
            chosen_run_id=str(d.get("chosen_run_id") or ""),
            value=d.get("value"),
            rationale_code=str(d.get("rationale_code") or ""),
            rationale_text=str(d.get("rationale_text") or ""),
            evidence_refs=list(d.get("evidence_refs") or []),
            by=str(d.get("by") or ""),
            at=str(d.get("at") or ""),
        )


@dataclass
class ColumnReview:
    """One column's review state in the checkpoint."""
    column: str
    status: str = "pending"               # pending | approved | edited | rejected | needs_review | no_action
    approved: dict[str, Any] = field(default_factory=dict)  # snapshot: pii_flag, tags, classification, definition, glossary, profiling_features
    reviewed_by: str = ""
    reviewed_at: str = ""
    note: str = ""
    verdicts: dict[str, FieldVerdict] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "column": self.column,
            "status": self.status,
            "approved": dict(self.approved),
            "reviewed_by": self.reviewed_by,
            "reviewed_at": self.reviewed_at,
            "note": self.note,
            "verdicts": {k: v.to_dict() for k, v in self.verdicts.items()},
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ColumnReview":
        raw_verdicts = d.get("verdicts") or {}
        verdicts = {
            k: FieldVerdict.from_dict(v) if isinstance(v, dict) else v
            for k, v in raw_verdicts.items()
        }
        status = str(d.get("status") or "pending")
        if status not in VALID_STATUSES:
            status = "pending"
        return cls(
            column=str(d.get("column") or ""),
            status=status,
            approved=dict(d.get("approved") or {}),
            reviewed_by=str(d.get("reviewed_by") or ""),
            reviewed_at=str(d.get("reviewed_at") or ""),
            note=str(d.get("note") or ""),
            verdicts=verdicts,
        )


@dataclass
class TableReview:
    """Table-level review items (name, description, owner, quality rules)."""
    items: dict[str, FieldVerdict] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"items": {k: v.to_dict() for k, v in self.items.items()}}

    @classmethod
    def from_dict(cls, d: dict) -> "TableReview":
        raw = (d or {}).get("items") or d or {}
        items = {}
        if isinstance(raw, dict):
            for k, v in raw.items():
                if isinstance(v, dict):
                    items[k] = FieldVerdict.from_dict(v)
        return cls(items=items)


@dataclass
class ReviewState:
    """The whole checkpoint for one contract."""
    table: str
    contract_uuid: str = ""
    columns: dict[str, ColumnReview] = field(default_factory=dict)
    total_columns: int = 0
    fully_approved: bool = False
    guaranteed: bool = False
    updated_at: str = ""
    updated_by: str = ""
    table_review: TableReview = field(default_factory=TableReview)

    @property
    def approved_count(self) -> int:
        """Legacy: approved + edited only (old readers)."""
        return sum(1 for c in self.columns.values() if c.status in ("approved", "edited"))

    @property
    def reviewed_count(self) -> int:
        return sum(1 for c in self.columns.values() if c.status in REVIEWED_STATUSES)

    def to_dict(self) -> dict:
        return {
            "table": self.table,
            "contract_uuid": self.contract_uuid,
            "columns": {k: v.to_dict() for k, v in self.columns.items()},
            "approved_count": self.approved_count,
            "reviewed_count": self.reviewed_count,
            "total_columns": self.total_columns,
            "fully_approved": self.fully_approved,
            "guaranteed": self.guaranteed,
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
            "table_review": self.table_review.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ReviewState":
        cols = {k: ColumnReview.from_dict(v) for k, v in (d.get("columns") or {}).items()}
        table_review = TableReview.from_dict(d.get("table_review") or {})
        return cls(
            table=str(d.get("table") or ""),
            contract_uuid=str(d.get("contract_uuid") or ""),
            columns=cols,
            total_columns=int(d.get("total_columns") or 0),
            fully_approved=bool(d.get("fully_approved") or False),
            guaranteed=bool(d.get("guaranteed") or False),
            updated_at=str(d.get("updated_at") or ""),
            updated_by=str(d.get("updated_by") or ""),
            table_review=table_review,
        )


def _columns_ready(state: ReviewState) -> bool:
    if not state.total_columns:
        return False
    if len(state.columns) < state.total_columns:
        # pending columns may be absent from the overlay entirely
        return False
    return all(c.status in REVIEWED_STATUSES for c in state.columns.values())


def _table_items_ready(state: ReviewState, required_items: Optional[list[str]] = None) -> bool:
    items = required_items if required_items is not None else list(state.table_review.items)
    if not items:
        # No table-level items recorded yet — they participate once seeded.
        return True
    for key in items:
        verdict = state.table_review.items.get(key)
        if verdict is None or verdict.decision not in REVIEWED_DECISIONS:
            return False
    return True


class ReviewStore:
    """Reads/writes the per-contract Final-Review checkpoint under ``_meta/reviews/``."""

    PREFIX = "_meta/reviews"

    def __init__(self, backend: StorageBackend, bucket: str):
        self.backend = backend
        self.bucket = bucket

    def _key(self, table: str) -> str:
        return f"{self.PREFIX}/{table}.json"

    def get(self, table: str) -> ReviewState:
        key = self._key(table)
        if not self.backend.exists(self.bucket, key):
            return ReviewState(table=table)
        return ReviewState.from_dict(self.backend.get_json(self.bucket, key) or {"table": table})

    def set_column(
        self,
        table: str,
        review: ColumnReview,
        *,
        total_columns: Optional[int] = None,
        contract_uuid: str = "",
        updated_by: str = "",
        required_table_items: Optional[list[str]] = None,
    ) -> ReviewState:
        if review.status not in VALID_STATUSES:
            raise ValueError(f"status must be one of {sorted(VALID_STATUSES)}")
        _scrub_column_review(review)
        state = self.get(table)
        if contract_uuid:
            state.contract_uuid = contract_uuid
        if total_columns is not None:
            state.total_columns = total_columns
        if not review.reviewed_at:
            review.reviewed_at = _utc_now_iso()
        state.columns[review.column] = review
        state.updated_at = _utc_now_iso()
        state.updated_by = updated_by or review.reviewed_by
        self._refresh_flags(state, required_table_items=required_table_items)
        self._save(table, state)
        return state

    def set_table_item(
        self,
        table: str,
        item: str,
        verdict: FieldVerdict,
        *,
        updated_by: str = "",
        required_table_items: Optional[list[str]] = None,
    ) -> ReviewState:
        _scrub_field_verdict(verdict)
        state = self.get(table)
        if not verdict.at:
            verdict.at = _utc_now_iso()
        state.table_review.items[item] = verdict
        state.updated_at = _utc_now_iso()
        state.updated_by = updated_by or verdict.by
        self._refresh_flags(state, required_table_items=required_table_items)
        self._save(table, state)
        return state

    def reset_column(self, table: str, column: str) -> ReviewState:
        state = self.get(table)
        if column in state.columns:
            del state.columns[column]
            state.fully_approved = False
            state.guaranteed = False
            state.updated_at = _utc_now_iso()
            self._save(table, state)
        return state

    def finalize(
        self,
        table: str,
        *,
        updated_by: str = "",
        required_table_items: Optional[list[str]] = None,
        required_columns: Optional[list[str]] = None,
    ) -> ReviewState:
        """Mark fully approved / guaranteed when every required item is reviewed."""
        state = self.get(table)
        if required_columns:
            state.total_columns = max(state.total_columns, len(required_columns))
        self._refresh_flags(state, required_table_items=required_table_items,
                            required_columns=required_columns)
        state.updated_at = _utc_now_iso()
        if updated_by:
            state.updated_by = updated_by
        self._save(table, state)
        return state

    def guarantee_blockers(
        self,
        table: str,
        *,
        required_columns: Optional[list[str]] = None,
        required_table_items: Optional[list[str]] = None,
    ) -> dict[str, list[str]]:
        state = self.get(table)
        blockers: dict[str, list[str]] = {
            "needs_review": [],
            "rejected": [],
            "pending": [],
            "table": [],
        }
        columns = required_columns or list(state.columns)
        seen = set()
        for name in columns:
            seen.add(name)
            cr = state.columns.get(name)
            status = cr.status if cr else "pending"
            if status == "needs_review":
                blockers["needs_review"].append(name)
            elif status == "rejected":
                blockers["rejected"].append(name)
            elif status not in REVIEWED_STATUSES:
                blockers["pending"].append(name)
        if required_columns:
            for name in required_columns:
                if name not in seen:
                    blockers["pending"].append(name)
        items = required_table_items if required_table_items is not None else list(state.table_review.items)
        for key in items:
            verdict = state.table_review.items.get(key)
            if verdict is None or verdict.decision not in REVIEWED_DECISIONS:
                blockers["table"].append(key)
        return blockers

    def clear(self, table: str) -> None:
        key = self._key(table)
        if self.backend.exists(self.bucket, key):
            self.backend.delete(self.bucket, key)

    def _refresh_flags(
        self,
        state: ReviewState,
        *,
        required_table_items: Optional[list[str]] = None,
        required_columns: Optional[list[str]] = None,
    ) -> None:
        if required_columns:
            state.total_columns = max(state.total_columns, len(required_columns))
            cols_ok = all(
                (state.columns.get(c) and state.columns[c].status in REVIEWED_STATUSES)
                for c in required_columns
            )
        else:
            cols_ok = _columns_ready(state)
        table_ok = _table_items_ready(state, required_table_items)
        state.guaranteed = bool(cols_ok and table_ok)
        # fully_approved keeps working for old readers: approved+edited covering every column.
        if state.total_columns and state.approved_count >= state.total_columns and table_ok:
            state.fully_approved = True
        elif state.guaranteed:
            # no_action columns count toward the guarantee but not the legacy approved_count
            state.fully_approved = cols_ok and table_ok
        else:
            state.fully_approved = False

    def _save(self, table: str, state: ReviewState) -> None:
        self.backend.put_json(self.bucket, self._key(table), state.to_dict())
