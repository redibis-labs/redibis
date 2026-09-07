"""Tests for ScanContractWriter build/persist."""

from __future__ import annotations

from unittest.mock import MagicMock

from redibis.scan.contract_writer import ScanContractWriter
from redibis.scan.types import ContractDraft, ScanRunResult
from redibis.services.scan_service import ScanConfig
from redibis.store.contract_store import ContractStore
from redibis.store.storage_backend import LocalBackend
from redibis.store.subcontract_store import SubcontractStore


def test_build_is_pure_no_io():
    result = ScanRunResult(
        run_id="run_1",
        table="telecom.customers",
        quality_contract={"quality": []},
        pii_contract={"schema": []},
        quality_expectations=5,
        quality_passed=4,
        quality_failed=1,
        pii_columns_scanned=10,
        pii_columns_detected=2,
    )
    draft = ScanContractWriter.build(result)

    assert isinstance(draft, ContractDraft)
    assert draft.table == "telecom.customers"
    assert draft.run_id == "run_1"
    assert draft.quality_partial == {"quality": []}
    assert draft.pii_partial == {"schema": []}
    assert draft.pii_summary["columns_detected"] == 2
    assert draft.quality_summary["passed"] == 4


def test_persist_writes_subcontracts_and_automerges():
    draft = ContractDraft(
        table="telecom.customers",
        run_id="run_1",
        pii_partial={"schema": []},
        quality_partial={"quality": []},
        pii_summary={"columns_scanned": 1, "columns_detected": 0},
        quality_summary={"expectations": 1, "passed": 1, "failed": 0},
    )
    sub_store = MagicMock()
    merger = MagicMock()
    store = MagicMock()
    upsert = MagicMock(version_after="v2")
    merger.merge_run.return_value = MagicMock(upsert=upsert)

    config = ScanConfig(table="telecom.customers", automerge="both")
    persist = ScanContractWriter.persist(
        draft,
        sub_store=sub_store,
        store=store,
        merger=merger,
        config=config,
        validate=True,
    )

    assert sub_store.create_from_payload.call_count == 2
    assert merger.merge_run.call_count == 2
    assert persist.pii_version == "v2"
    assert persist.quality_version == "v2"


def test_write_kind_and_merge_kind_roundtrip(tmp_path):
    from redibis.store.run_merger import RunMerger

    backend = LocalBackend(str(tmp_path / "storage"))
    store = ContractStore(backend, bucket="active-contracts")
    sub_store = SubcontractStore(backend)
    merger = RunMerger(store, sub_store)

    payload = {"apiVersion": "v3.0.1", "schema": [{"physicalName": "db.t"}]}
    sub = ScanContractWriter.write_kind(
        "quality",
        table="db.t",
        run_id="run-1",
        payload=payload,
        sub_store=sub_store,
    )
    assert sub.kind == "quality"
    assert sub_store.get("quality", "db.t", "run-1") is not None

    store.upsert(payload, "db.t", workflow="quality")
    result = ScanContractWriter.merge_kind(
        "quality", "db.t", "run-1", merger=merger, validate=False,
    )
    assert result.upsert.version_after


def test_persist_subcontracts_only_when_no_automerge():
    draft = ContractDraft(
        table="telecom.customers",
        run_id="run_1",
        quality_partial={"quality": []},
    )
    sub_store = MagicMock()
    config = ScanConfig(table="telecom.customers", automerge="none")

    persist = ScanContractWriter.persist(
        draft,
        sub_store=sub_store,
        config=config,
    )

    sub_store.create_from_payload.assert_called_once()
    assert persist.quality_version is None
    assert persist.pii_version is None


def test_persist_session_kind_automerge(tmp_path):
    from redibis.store.run_merger import RunMerger

    backend = LocalBackend(str(tmp_path / "storage"))
    store = ContractStore(backend, bucket="active-contracts")
    sub_store = SubcontractStore(backend)
    merger = RunMerger(store, sub_store)
    payload = {"apiVersion": "v3.0.1", "schema": [{"physicalName": "db.t"}]}
    store.upsert(payload, "db.t", workflow="quality")

    sub, upsert = ScanContractWriter.persist_session_kind(
        "quality",
        table="db.t",
        run_id="run-2",
        payload=payload,
        sub_store=sub_store,
        store=store,
        merger=merger,
        automerge=True,
        validate=False,
    )
    assert sub.kind == "quality"
    assert upsert is not None
    assert upsert.version_after
