"""Build and persist ODCS partials from scan results."""

from __future__ import annotations

from typing import Optional

from redibis.scan.types import ContractDraft, ContractPersistResult, ScanRunResult
from redibis.scan.config import ScanConfig
from redibis.store.run_merger import RunMerger
from redibis.store.subcontract_store import SubcontractStore


class ScanContractWriter:
    """Build ODCS partials from scan results and persist subcontracts / automerge."""

    @staticmethod
    def build(result: ScanRunResult) -> ContractDraft:
        pii_summary = {
            "columns_scanned": result.pii_columns_scanned,
            "columns_detected": result.pii_columns_detected,
        }
        quality_summary = {
            "expectations": result.quality_expectations,
            "passed": result.quality_passed,
            "failed": result.quality_failed,
        }
        return ContractDraft(
            table=result.table,
            run_id=result.run_id,
            schema_partial=result.schema_contract,
            pii_partial=result.pii_contract,
            quality_partial=result.quality_contract,
            pii_summary=pii_summary,
            quality_summary=quality_summary,
        )

    @staticmethod
    def write_kind(
        kind: str,
        *,
        table: str,
        run_id: str,
        payload: dict,
        sub_store: SubcontractStore,
        contract_uuid: Optional[str] = None,
        summary_stats: Optional[dict] = None,
    ):
        """Write a single kind subcontract (session/UI path)."""
        return sub_store.create_from_payload(
            kind=kind,
            schema_table=table,
            run_id=run_id,
            payload=payload,
            contract_uuid=contract_uuid,
            summary_stats=summary_stats or {},
        )

    @staticmethod
    def merge_kind(
        kind: str,
        table: str,
        run_id: str,
        *,
        merger: RunMerger,
        validate: bool = True,
    ):
        """Merge one subcontract into the active contract."""
        return merger.merge_run(kind, table, run_id, validate=validate)

    @staticmethod
    def write_subcontracts(
        draft: ContractDraft,
        sub_store: SubcontractStore,
        *,
        contract_uuid: Optional[str] = None,
    ) -> None:
        if draft.pii_partial:
            sub_store.create_from_payload(
                kind="pii",
                schema_table=draft.table,
                run_id=draft.run_id,
                payload=draft.pii_partial,
                contract_uuid=contract_uuid,
                summary_stats={
                    "pii_confirmed": draft.pii_summary.get("columns_detected", 0),
                    "total_columns": draft.pii_summary.get("columns_scanned", 0),
                },
            )
        if draft.quality_partial:
            sub_store.create_from_payload(
                kind="quality",
                schema_table=draft.table,
                run_id=draft.run_id,
                payload=draft.quality_partial,
                contract_uuid=contract_uuid,
                summary_stats={
                    "quality_passed": draft.quality_summary.get("passed", 0),
                    "quality_total": draft.quality_summary.get("expectations", 0),
                },
            )

    @staticmethod
    def merge_active(
        draft: ContractDraft,
        *,
        store,
        sub_store: SubcontractStore,
        merger: RunMerger,
        config: ScanConfig,
        validate: bool = True,
    ) -> ContractPersistResult:
        out = ContractPersistResult()
        automerge_active = (config.automerge or "").lower() not in ("", "none")
        # Automated scan automerge → strip value-bearing quality rules off PII
        # columns so raw PII values never land in the contract (CLI/agent path).
        if draft.schema_partial and store is not None and automerge_active:
            res = store.upsert(
                partial=draft.schema_partial,
                table=draft.table,
                workflow="schema",
                run_id=draft.run_id,
                validate=validate,
                strip_pii_quality=True,
            )
            out.schema_version = res.version_after
        if draft.pii_partial and config.automerges("pii"):
            res = merger.merge_run(
                "pii",
                draft.table,
                draft.run_id,
                validate=validate,
                strip_pii_quality=True,
            )
            out.pii_version = res.upsert.version_after
        if draft.quality_partial and config.automerges("quality"):
            res = merger.merge_run(
                "quality",
                draft.table,
                draft.run_id,
                validate=validate,
                strip_pii_quality=True,
            )
            out.quality_version = res.upsert.version_after
        return out

    @classmethod
    def persist(
        cls,
        draft: ContractDraft,
        *,
        sub_store: SubcontractStore,
        store=None,
        merger: Optional[RunMerger] = None,
        config: Optional[ScanConfig] = None,
        contract_uuid: Optional[str] = None,
        validate: bool = True,
    ) -> ContractPersistResult:
        """Write subcontracts; optionally automerge into active contract."""
        cls.write_subcontracts(draft, sub_store, contract_uuid=contract_uuid)
        if (
            config
            and store is not None
            and merger is not None
            and (config.automerge or "").lower() not in ("", "none")
        ):
            return cls.merge_active(
                draft,
                store=store,
                sub_store=sub_store,
                merger=merger,
                config=config,
                validate=validate,
            )
        return ContractPersistResult()

    @staticmethod
    def persist_session_kind(
        kind: str,
        *,
        table: str,
        run_id: str,
        payload: dict,
        sub_store: SubcontractStore,
        store,
        merger: Optional[RunMerger] = None,
        contract_uuid: Optional[str] = None,
        summary_stats: Optional[dict] = None,
        automerge: bool = False,
        validate: bool = True,
    ):
        """
        Session/UI path: write one subcontract and optionally merge into active.

        Returns ``(subcontract, upsert_result_or_none)``.
        """
        sub = ScanContractWriter.write_kind(
            kind,
            table=table,
            run_id=run_id,
            payload=payload,
            sub_store=sub_store,
            contract_uuid=contract_uuid,
            summary_stats=summary_stats,
        )
        upsert = None
        if automerge:
            if merger is None:
                merger = RunMerger(store, sub_store)
            result = ScanContractWriter.merge_kind(
                kind,
                table,
                run_id,
                merger=merger,
                validate=validate,
            )
            upsert = result.upsert
        return sub, upsert


def __getattr__(name: str):
    if name == "ContractDraftService":
        import warnings
        warnings.warn(
            "ContractDraftService is renamed to ScanContractWriter",
            DeprecationWarning,
            stacklevel=2,
        )
        return ScanContractWriter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
