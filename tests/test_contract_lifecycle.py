"""Integration tests for contract lifecycle (deterministic → LLM auto-write)."""

from datetime import datetime, timezone

import pytest

from redibis.enrich.providers import EnrichmentProvider
from redibis.enrich.service import EnrichmentService
from redibis.store.contract_metadata import ContractMetadataStore
from redibis.store.contract_store import ContractStore
from redibis.store.run_output_writer import RunOutputWriter
from redibis.store.storage_backend import LocalBackend


class FakeProvider(EnrichmentProvider):
    def __init__(self, response: dict):
        super().__init__(model="fake-1")
        self.name = "fake"
        self._response = response

    def complete(self, system_prompt, user_prompt, *, json_mode=True):
        import json as _json
        return _json.dumps(self._response)


@pytest.fixture
def store(tmp_path):
    backend = LocalBackend(tmp_path / "storage")
    return ContractStore(backend, bucket="active-contracts")


def _seed_deterministic(store, *, email_pii=True):
    email_col = {"name": "email", "logicalType": "string", "tags": ["pii"]}
    if email_pii:
        email_col.update({
            "classification": "pii_personal",
            "privacy": {"classification_engine": {"entity_type": "EMAIL", "detected": True}},
        })
    contract = {
        "apiVersion": "v3.0.1", "kind": "DataContract",
        "name": "telecom_customers_contract", "version": "1.0.0", "status": "active",
        "schema": [{
            "name": "telecom_customers", "physicalName": "telecom.customers",
            "properties": [
                email_col,
                {"name": "city", "logicalType": "string"},
            ],
        }],
    }
    store.upsert(contract, table="telecom.customers", workflow="manual", run_id="seed_det")
    return contract


def _run_writer(store, table="telecom.customers", run_id="test_run"):
    return RunOutputWriter(
        backend=store.backend,
        bucket="pii-reports",
        workflow="enrich",
        table=table,
        run_id=run_id,
    )


def test_enrich_auto_writes_and_run_artifacts(store):
    _seed_deterministic(store)
    svc = EnrichmentService(store)
    run_id = "enrich_run_1"
    writer = _run_writer(store, run_id=run_id)
    provider = FakeProvider({
        "columns": {
            "email": {
                "business": {"definition": "Customer email address"},
                "pii": {"classification": "none"},
            },
            "city": {"business": {"definition": "City of residence"}},
        },
    })
    result = svc.enrich(
        "telecom.customers", provider, run_writer=writer, run_id=run_id,
    )
    assert result.valid
    assert result.auto_written
    assert svc.get_candidate("telecom.customers") is None

    active = store.get_active("telecom.customers")
    city = next(p for p in active["schema"][0]["properties"] if p["name"] == "city")
    assert city["business"]["definition"] == "City of residence"

    prefix = f"enrich/telecom_customers/{run_id}"
    bucket = "pii-reports"
    for name in (
        "contract.llm.yaml",
        "contract_diff.json",
        "contract_diff.md",
        "contract.active.ref.json",
    ):
        assert store.backend.exists(bucket, f"{prefix}/{name}")

    ref = store.backend.get_json(bucket, f"{prefix}/contract.active.ref.json")
    assert ref["source"] == "llm"
    assert ref["run_id"] == run_id


def test_llm_classification_persists_through_subsequent_upsert(store):
    _seed_deterministic(store)
    svc = EnrichmentService(store)
    provider = FakeProvider({
        "columns": {
            "email": {
                "business": {"definition": "Not personal"},
                "pii": {"classification": "none"},
            },
        },
    })
    result = svc.enrich("telecom.customers", provider, run_writer=_run_writer(store))
    assert result.auto_written

    active = store.get_active("telecom.customers")
    email = next(p for p in active["schema"][0]["properties"] if p["name"] == "email")
    from redibis.contracts.privacy import column_is_pii
    assert not column_is_pii(email)

    store.upsert(active, table="telecom.customers", workflow="schema", run_id="later_scan")
    active2 = store.get_active("telecom.customers")
    email2 = next(p for p in active2["schema"][0]["properties"] if p["name"] == "email")
    assert not column_is_pii(email2)


def test_deterministic_in_audit_after_llm_enrich(store):
    _seed_deterministic(store)
    det_version = store.get_active("telecom.customers")["version"]
    svc = EnrichmentService(store)
    provider = FakeProvider({
        "columns": {"city": {"business": {"definition": "City"}}},
    })
    svc.enrich("telecom.customers", provider, run_writer=_run_writer(store))
    history = store.get_history("telecom.customers", limit=20)
    versions = [h.version for h in history]
    assert det_version in versions
    active = store.get_active("telecom.customers")
    city = next(p for p in active["schema"][0]["properties"] if p["name"] == "city")
    assert city["business"]["definition"] == "City"


def test_deterministic_snapshot_after_automerge_scan(store):
    from dataclasses import replace

    from redibis.contracts.lifecycle import write_deterministic_snapshot
    from redibis.scan.contract_writer import ScanContractWriter
    from redibis.scan.types import ContractDraft
    from redibis.services.scan_service import ScanConfig
    from redibis.store.run_merger import RunMerger
    from redibis.store.subcontract_store import SubcontractStore

    _seed_deterministic(store)
    c_det_before = store.get_active("telecom.customers")
    run_id = "scan_det_1"
    writer = RunOutputWriter(
        backend=store.backend,
        bucket="pii-reports",
        workflow="scan",
        table="telecom.customers",
        run_id=run_id,
    )
    sub_store = SubcontractStore(store.backend, "pii-contracts", "quality-contracts")
    merger = RunMerger(store, sub_store)
    draft = ContractDraft(
        table="telecom.customers",
        run_id=run_id,
        schema_partial=c_det_before,
    )
    config = replace(ScanConfig(table="telecom.customers"), automerge="both")
    ScanContractWriter.persist(
        draft, sub_store=sub_store, store=store, merger=merger, config=config,
    )
    c_det = store.get_active("telecom.customers")
    write_deterministic_snapshot(writer, c_det, run_id=run_id)
    prefix = f"scan/telecom_customers/{run_id}"
    assert store.backend.exists("pii-reports", f"{prefix}/contract.deterministic.yaml")
    ref = store.backend.get_json("pii-reports", f"{prefix}/contract.active.ref.json")
    assert ref["source"] == "deterministic"


def test_phonenumbers_disabled_recorded_in_provenance(store):
    from dataclasses import replace

    from redibis.services.scan_service import ScanConfig, ScanService

    scan_cfg = replace(ScanConfig(table="telecom.customers", automerge="both"), use_phonenumbers=False)
    svc = ScanService(backend=store.backend, store=store)
    run_id = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    writer = RunOutputWriter(
        backend=store.backend,
        bucket="pii-reports",
        workflow="scan",
        table="telecom.customers",
        run_id=run_id,
    )
    _seed_deterministic(store)
    svc._write_deterministic_lifecycle(
        run_writer=writer,
        table="telecom.customers",
        run_id=run_id,
        scan_config=scan_cfg,
    )
    meta = ContractMetadataStore(store.backend, store.bucket)
    prov = meta.get_provenance("telecom.customers")
    assert prov
    last = prov[-1]
    assert last.get("engines", {}).get("phonenumbers") == "disabled"
