"""
redibis.store.run_merger — the v2 "select-and-merge" step.

Scan no longer auto-upserts into the active contract. Instead each run writes a
subcontract into its type bucket (pii-contracts / quality-contracts). This
module is the bridge that lets a caller:

    1. list the runs in a type bucket            → list_runs / list_run_summaries
    2. inspect one run                           → get_run
    3. edit a run's payload before merging       → edit_run
    4. merge a chosen run into the active         → merge_run
    5. discard a run (never merge it)            → discard_run

Merging folds the run's payload into the single active contract via
ContractStore.upsert() (still the only writer to the active bucket), then marks
the subcontract ``merged`` and stamps the active contract_uuid back onto it.

``automerge`` (scan-time toggle) is just: write the subcontract, then
immediately merge_run it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from redibis.store.contract_store import ContractStore, UpsertResult
from redibis.store.subcontract_store import (
    EditRecord,
    Subcontract,
    SubcontractStore,
    STATUS_MERGED,
    VALID_KINDS,
)


@dataclass
class MergeResult:
    """Result of merging one run subcontract into the active contract."""
    kind: str
    table: str
    run_id: str
    subcontract_id: str
    upsert: UpsertResult

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "table": self.table,
            "run_id": self.run_id,
            "subcontract_id": self.subcontract_id,
            **self.upsert.to_dict(),
        }


class RunMerger:
    """Select-and-merge orchestrator over SubcontractStore + ContractStore."""

    def __init__(self, store: ContractStore, sub_store: SubcontractStore):
        self.store = store
        self.sub_store = sub_store

    # ── Inspect ──────────────────────────────────────────────────────────

    def list_runs(self, kind: str, table: str) -> list[Subcontract]:
        return self.sub_store.list_runs(kind, table)

    def list_run_summaries(self, kind: str, table: str) -> list[dict]:
        return self.sub_store.list_run_summaries(kind, table)

    def get_run(self, kind: str, table: str, run_id: str) -> Optional[Subcontract]:
        return self.sub_store.get(kind, table, run_id)

    # ── Edit before merge ─────────────────────────────────────────────────

    def edit_run(self, kind: str, table: str, run_id: str, payload: dict,
                 note: str = "", edited_by: str = "system") -> Optional[Subcontract]:
        edit = EditRecord(field="payload", note=note or "edited before merge",
                          edited_by=edited_by)
        return self.sub_store.update_payload(kind, table, run_id, payload, edit=edit)

    # ── Merge / discard ───────────────────────────────────────────────────

    def merge_run(self, kind: str, table: str, run_id: str,
                  validate: bool = True,
                  strip_pii_quality: bool = False) -> MergeResult:
        """Fold the chosen run's payload into the active contract.

        ``strip_pii_quality`` (default False) is forwarded to
        ``ContractStore.upsert``. Automated scan generators (CLI scan
        automerge, ``redibis runs merge``) pass True so PII columns lose their
        value-bearing quality rules; the web UI manual merge keeps the default
        False so a human can retain them.
        """
        if kind not in VALID_KINDS:
            raise ValueError(f"Unknown kind {kind!r}; expected one of {VALID_KINDS}")
        sub = self.sub_store.get(kind, table, run_id)
        if sub is None:
            raise KeyError(f"No {kind} run {run_id!r} for table {table!r}")
        upsert = self.store.upsert(
            partial=dict(sub.payload),
            table=table,
            workflow=kind,
            run_id=run_id,
            validate=validate,
            strip_pii_quality=strip_pii_quality,
        )
        # Mark merged + stamp the active contract_uuid onto the subcontract.
        self.sub_store.set_status(kind, table, run_id, STATUS_MERGED,
                                  contract_uuid=upsert.contract_uuid)
        return MergeResult(kind=kind, table=table, run_id=run_id,
                           subcontract_id=sub.subcontract_id, upsert=upsert)

    def discard_run(self, kind: str, table: str, run_id: str) -> Optional[Subcontract]:
        return self.sub_store.discard(kind, table, run_id)

    # ── Batch (CLI merge --batch) ──────────────────────────────────────────

    def merge_latest(self, kind: str, table: str, validate: bool = True,
                     run_id: Optional[str] = None,
                     strip_pii_quality: bool = False) -> Optional[MergeResult]:
        """Merge a chosen run (or the latest non-discarded run) for a table."""
        if run_id is not None:
            return self.merge_run(kind, table, run_id, validate=validate,
                                  strip_pii_quality=strip_pii_quality)
        runs = [s for s in self.sub_store.list_runs(kind, table)
                if s.status != "discarded"]
        if not runs:
            return None
        # list_runs is newest-first
        return self.merge_run(kind, table, runs[0].run_id, validate=validate,
                              strip_pii_quality=strip_pii_quality)
