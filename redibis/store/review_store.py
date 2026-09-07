"""
redibis.store.review_store
==========================
Final-Review checkpoint overlay — the resumable record of which columns of a contract a
human has approved (or edited/rejected), stored OUTSIDE the contract document under the
``_meta/`` namespace (same pattern as PiiDecisionStore / DefinitionDecisionStore).

Storage layout (contracts bucket, i.e. MinIO/S3 or local):
    _meta/reviews/{db}.{table}.json

The contract YAML itself is never modified by approval — only edits go through
``ContractStore.upsert``; approval is logged here. ``fully_approved`` flips true when every
column in the contract is approved (or via explicit finalize). Reopening a contract resumes
from this overlay.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from redibis.store.storage_backend import StorageBackend

VALID_STATUSES = {"pending", "approved", "edited", "rejected"}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ColumnReview:
    """One column's review state in the checkpoint."""
    column: str
    status: str = "pending"               # pending | approved | edited | rejected
    approved: dict[str, Any] = field(default_factory=dict)  # snapshot: pii_flag, tags, classification, definition, glossary, profiling_features
    reviewed_by: str = ""
    reviewed_at: str = ""
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "column": self.column,
            "status": self.status,
            "approved": dict(self.approved),
            "reviewed_by": self.reviewed_by,
            "reviewed_at": self.reviewed_at,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ColumnReview":
        return cls(
            column=str(d.get("column") or ""),
            status=str(d.get("status") or "pending"),
            approved=dict(d.get("approved") or {}),
            reviewed_by=str(d.get("reviewed_by") or ""),
            reviewed_at=str(d.get("reviewed_at") or ""),
            note=str(d.get("note") or ""),
        )


@dataclass
class ReviewState:
    """The whole checkpoint for one contract."""
    table: str
    contract_uuid: str = ""
    columns: dict[str, ColumnReview] = field(default_factory=dict)
    total_columns: int = 0
    fully_approved: bool = False
    updated_at: str = ""
    updated_by: str = ""

    @property
    def approved_count(self) -> int:
        return sum(1 for c in self.columns.values() if c.status in ("approved", "edited"))

    def to_dict(self) -> dict:
        return {
            "table": self.table,
            "contract_uuid": self.contract_uuid,
            "columns": {k: v.to_dict() for k, v in self.columns.items()},
            "approved_count": self.approved_count,
            "total_columns": self.total_columns,
            "fully_approved": self.fully_approved,
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ReviewState":
        cols = {k: ColumnReview.from_dict(v) for k, v in (d.get("columns") or {}).items()}
        return cls(
            table=str(d.get("table") or ""),
            contract_uuid=str(d.get("contract_uuid") or ""),
            columns=cols,
            total_columns=int(d.get("total_columns") or 0),
            fully_approved=bool(d.get("fully_approved") or False),
            updated_at=str(d.get("updated_at") or ""),
            updated_by=str(d.get("updated_by") or ""),
        )


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
    ) -> ReviewState:
        if review.status not in VALID_STATUSES:
            raise ValueError(f"status must be one of {sorted(VALID_STATUSES)}")
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
        # auto-flip fully_approved
        if state.total_columns and state.approved_count >= state.total_columns:
            state.fully_approved = True
        self._save(table, state)
        return state

    def reset_column(self, table: str, column: str) -> ReviewState:
        state = self.get(table)
        if column in state.columns:
            del state.columns[column]
            state.fully_approved = False
            state.updated_at = _utc_now_iso()
            self._save(table, state)
        return state

    def finalize(self, table: str, *, updated_by: str = "") -> ReviewState:
        """Explicitly mark fully approved (only if every column is approved)."""
        state = self.get(table)
        if state.total_columns and state.approved_count >= state.total_columns:
            state.fully_approved = True
        state.updated_at = _utc_now_iso()
        if updated_by:
            state.updated_by = updated_by
        self._save(table, state)
        return state

    def clear(self, table: str) -> None:
        key = self._key(table)
        if self.backend.exists(self.bucket, key):
            self.backend.delete(self.bucket, key)

    def _save(self, table: str, state: ReviewState) -> None:
        self.backend.put_json(self.bucket, self._key(table), state.to_dict())
