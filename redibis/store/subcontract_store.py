"""
redibis.store.subcontract_store — v2 run-scoped subcontract storage.

A *subcontract* is a partial ODCS contract produced by exactly one scan run.
Each run writes one object into its **type-specific bucket**, never directly
into the active contract. You later select one run and merge it into the
single active contract (see redibis.store.run_merger).

Two dedicated run buckets (the locked v2 decision):

    pii-contracts/       {schema}.{table}/{run_id}.yaml   — one per PII run
    quality-contracts/   {schema}.{table}/{run_id}.yaml   — one per quality run

So a table accumulates a history of PII runs and a history of quality runs.
A bad run is simply never selected (and purge wipes the active if poisoned).

Lifecycle:  draft → (edit) → reviewed → (merge) → merged   ; or discarded.

This module is the ONLY writer to the two run buckets. It never touches the
active-contracts bucket (that is ContractStore's job).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from redibis.store.storage_backend import StorageBackend


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

KIND_PII = "pii"
KIND_QUALITY = "quality"
VALID_KINDS = (KIND_PII, KIND_QUALITY)

# Subcontract lifecycle states
STATUS_DRAFT = "draft"
STATUS_REVIEWED = "reviewed"
STATUS_MERGED = "merged"
STATUS_DISCARDED = "discarded"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _table_safe(table: str) -> str:
    """'telecom.customers' stays 'telecom.customers' (dot kept for clarity)."""
    return table


# ─────────────────────────────────────────────────────────────────────────────
# Edit record — one manual edit applied to a subcontract during review
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class EditRecord:
    """A single manual edit applied to a subcontract during review."""
    field: str                       # dotted path or human label, e.g. "column.email.pii.entity_type"
    note: str = ""                   # human-readable description of the edit
    edited_by: str = "system"
    edited_at: str = field(default_factory=_utc_now_iso)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "EditRecord":
        return cls(
            field=d.get("field", ""),
            note=d.get("note", ""),
            edited_by=d.get("edited_by", "system"),
            edited_at=d.get("edited_at", _utc_now_iso()),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Subcontract — the run-scoped partial contract object
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Subcontract:
    """
    A partial contract produced by one scan run, stored in a type bucket.

    See architecture v2 §1.3. The ``payload`` is an ODCS partial (a PII block
    OR a quality block) that gets folded into the active contract on merge.
    """
    subcontract_id: str
    contract_uuid: Optional[str]     # which active contract it targets (None until known)
    schema_table: str                # 'schema.table'
    kind: str                        # 'pii' | 'quality'
    run_id: str                      # the scan run that produced it
    payload: dict = field(default_factory=dict)
    status: str = STATUS_DRAFT
    created_at: str = field(default_factory=_utc_now_iso)
    reviewed_at: Optional[str] = None
    merged_at: Optional[str] = None
    created_by: str = "system"
    edits: list[EditRecord] = field(default_factory=list)
    summary_stats: dict = field(default_factory=dict)  # lightweight stats for list views

    # ── Serialization ──────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        d = asdict(self)
        d["edits"] = [e.to_dict() for e in self.edits]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Subcontract":
        return cls(
            subcontract_id=d.get("subcontract_id") or f"sub_{uuid.uuid4().hex[:12]}",
            contract_uuid=d.get("contract_uuid"),
            schema_table=d.get("schema_table", ""),
            kind=d.get("kind", ""),
            run_id=d.get("run_id", ""),
            payload=d.get("payload", {}) or {},
            status=d.get("status", STATUS_DRAFT),
            created_at=d.get("created_at", _utc_now_iso()),
            reviewed_at=d.get("reviewed_at"),
            merged_at=d.get("merged_at"),
            created_by=d.get("created_by", "system"),
            edits=[EditRecord.from_dict(e) for e in (d.get("edits") or [])],
            summary_stats=d.get("summary_stats", {}) or {},
        )

    def summary(self) -> dict:
        """Lightweight view (no full payload) for run-list rendering."""
        return {
            "subcontract_id": self.subcontract_id,
            "contract_uuid": self.contract_uuid,
            "schema_table": self.schema_table,
            "kind": self.kind,
            "run_id": self.run_id,
            "status": self.status,
            "created_at": self.created_at,
            "reviewed_at": self.reviewed_at,
            "merged_at": self.merged_at,
            "created_by": self.created_by,
            "edit_count": len(self.edits),
            "summary_stats": self.summary_stats,
        }


# ─────────────────────────────────────────────────────────────────────────────
# SubcontractStore — sole writer to the two run buckets
# ─────────────────────────────────────────────────────────────────────────────

class SubcontractStore:
    """
    Read/write subcontracts in the two dedicated run buckets.

    Keys:
        {schema}.{table}/{run_id}.yaml          — the subcontract object
        {schema}.{table}/_run_index.jsonl       — append-only run index

    The bucket is chosen by ``kind`` (pii → pii_bucket, quality → quality_bucket).
    """

    def __init__(
        self,
        backend: StorageBackend,
        pii_bucket: str = "pii-contracts",
        quality_bucket: str = "quality-contracts",
    ):
        self.backend = backend
        self.pii_bucket = pii_bucket
        self.quality_bucket = quality_bucket

    # ── Bucket / key helpers ─────────────────────────────────────────────

    def bucket_for(self, kind: str) -> str:
        if kind == KIND_PII:
            return self.pii_bucket
        if kind == KIND_QUALITY:
            return self.quality_bucket
        raise ValueError(f"Unknown subcontract kind {kind!r}; expected one of {VALID_KINDS}")

    def _run_key(self, table: str, run_id: str) -> str:
        return f"{_table_safe(table)}/{run_id}.yaml"

    def _index_key(self, table: str) -> str:
        return f"{_table_safe(table)}/_run_index.jsonl"

    # ── Write ─────────────────────────────────────────────────────────────

    def write(self, sub: Subcontract) -> str:
        """Persist a subcontract to its type bucket and append the run index."""
        if sub.kind not in VALID_KINDS:
            raise ValueError(f"Unknown subcontract kind {sub.kind!r}")
        bucket = self.bucket_for(sub.kind)
        key = self._run_key(sub.schema_table, sub.run_id)
        self.backend.put_yaml(bucket, key, sub.to_dict())
        self._append_index(sub)
        return key

    def create_from_payload(
        self,
        *,
        kind: str,
        schema_table: str,
        run_id: str,
        payload: dict,
        contract_uuid: Optional[str] = None,
        created_by: str = "system",
        summary_stats: Optional[dict] = None,
    ) -> Subcontract:
        """Build + persist a subcontract in one call. Returns the stored object."""
        sub = Subcontract(
            subcontract_id=f"sub_{kind}_{uuid.uuid4().hex[:12]}",
            contract_uuid=contract_uuid,
            schema_table=schema_table,
            kind=kind,
            run_id=run_id,
            payload=payload or {},
            created_by=created_by,
            summary_stats=summary_stats or {},
        )
        self.write(sub)
        return sub

    # ── Read ──────────────────────────────────────────────────────────────

    def get(self, kind: str, table: str, run_id: str) -> Optional[Subcontract]:
        bucket = self.bucket_for(kind)
        key = self._run_key(table, run_id)
        if not self.backend.exists(bucket, key):
            return None
        return Subcontract.from_dict(self.backend.get_yaml(bucket, key))

    def list_runs(self, kind: str, table: str) -> list[Subcontract]:
        """List every run subcontract for a table in the given bucket (newest first)."""
        bucket = self.bucket_for(kind)
        prefix = f"{_table_safe(table)}/"
        subs: list[Subcontract] = []
        for key in self.backend.list_keys(bucket, prefix=prefix):
            if not key.endswith(".yaml"):
                continue
            try:
                subs.append(Subcontract.from_dict(self.backend.get_yaml(bucket, key)))
            except Exception:
                continue
        subs.sort(key=lambda s: s.created_at, reverse=True)
        return subs

    def list_run_summaries(self, kind: str, table: str) -> list[dict]:
        return [s.summary() for s in self.list_runs(kind, table)]

    def list_tables(self, kind: str) -> list[str]:
        """List every schema.table that has at least one run in the bucket."""
        bucket = self.bucket_for(kind)
        tables: set[str] = set()
        for key in self.backend.list_keys(bucket):
            if "/" in key:
                tables.add(key.split("/", 1)[0])
        return sorted(tables)

    # ── Mutate ──────────────────────────────────────────────────────────────

    def update_payload(self, kind: str, table: str, run_id: str,
                        payload: dict, edit: Optional[EditRecord] = None) -> Optional[Subcontract]:
        """Replace a run's payload (edit before merge). Appends an EditRecord."""
        sub = self.get(kind, table, run_id)
        if sub is None:
            return None
        sub.payload = payload
        if edit is not None:
            sub.edits.append(edit)
        if sub.status == STATUS_DRAFT:
            sub.status = STATUS_REVIEWED
            sub.reviewed_at = _utc_now_iso()
        self.write(sub)
        return sub

    def set_status(self, kind: str, table: str, run_id: str, status: str,
                   contract_uuid: Optional[str] = None) -> Optional[Subcontract]:
        sub = self.get(kind, table, run_id)
        if sub is None:
            return None
        sub.status = status
        if status == STATUS_MERGED:
            sub.merged_at = _utc_now_iso()
            if contract_uuid:
                sub.contract_uuid = contract_uuid
        elif status == STATUS_REVIEWED and not sub.reviewed_at:
            sub.reviewed_at = _utc_now_iso()
        self.write(sub)
        return sub

    def discard(self, kind: str, table: str, run_id: str) -> Optional[Subcontract]:
        """Mark a run discarded (object retained for history)."""
        return self.set_status(kind, table, run_id, STATUS_DISCARDED)

    def delete_table_runs(self, table: str, kind: Optional[str] = None) -> dict[str, list[str]]:
        """Hard-delete every run object for a table (used by purge --no-keep-runs)."""
        kinds = (kind,) if kind else VALID_KINDS
        deleted: dict[str, list[str]] = {}
        for k in kinds:
            bucket = self.bucket_for(k)
            deleted[k] = self.backend.delete_prefix(bucket, f"{_table_safe(table)}/")
        return deleted

    # ── Internal: append-only run index ─────────────────────────────────────

    def _append_index(self, sub: Subcontract) -> None:
        """
        Maintain a per-table run index. Because re-writing the same run_id must
        not duplicate the index row, we rebuild the index from the row set
        keyed by run_id (the index stays small per table).
        """
        bucket = self.bucket_for(sub.kind)
        index_key = self._index_key(sub.schema_table)
        rows: dict[str, dict] = {}
        if self.backend.exists(bucket, index_key):
            text = self.backend.get_text(bucket, index_key)
            for line in text.strip().split("\n"):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    rows[row["run_id"]] = row
                except (json.JSONDecodeError, KeyError):
                    continue
        rows[sub.run_id] = {
            "subcontract_id": sub.subcontract_id,
            "run_id": sub.run_id,
            "kind": sub.kind,
            "status": sub.status,
            "created_at": sub.created_at,
            "merged_at": sub.merged_at,
            "summary_stats": sub.summary_stats,
        }
        body = "".join(json.dumps(r, ensure_ascii=False) + "\n"
                       for r in sorted(rows.values(), key=lambda r: r.get("created_at", "")))
        self.backend.put_text(bucket, index_key, body, content_type="application/x-ndjson")
