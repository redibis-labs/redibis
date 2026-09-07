"""Tests for the v2 contract architecture: subcontract buckets, select-and-merge,
purge, PIIColumnReport, and quality rules round-trip."""

import pytest

from redibis.store.storage_backend import LocalBackend
from redibis.store.contract_store import ContractStore
from redibis.store.subcontract_store import (
    SubcontractStore, Subcontract, STATUS_MERGED, STATUS_DISCARDED, STATUS_REVIEWED,
)
from redibis.store.run_merger import RunMerger
from redibis.models import PIIDetection, PIIColumnReport
from redibis.pii.equations import decide_pii, build_column_report
from redibis.pii.thresholds import Thresholds
from redibis.contracts.rules import extract_rules, odcs_to_ge, regenerate


@pytest.fixture
def backend(tmp_path):
    return LocalBackend(tmp_path / "storage")


@pytest.fixture
def store(backend):
    return ContractStore(backend, bucket="active-contracts")


@pytest.fixture
def sub_store(backend):
    return SubcontractStore(backend, pii_bucket="pii-contracts",
                            quality_bucket="quality-contracts")


def _pii_payload(table="telecom.customers"):
    return {
        "apiVersion": "v3.0.1", "kind": "DataContract",
        "name": "telecom_customers_contract",
        "schema": [{
            "name": "telecom_customers", "physicalName": table,
            "properties": [{
                "name": "phone", "classification": "pii_personal",
                "tags": ["pii"], "pii": {"detected": True, "entity_type": "PHONE_NUMBER",
                                          "confidence": 0.92},
            }],
        }],
    }


def _quality_payload(table="telecom.customers"):
    return {
        "apiVersion": "v3.0.1", "kind": "DataContract",
        "name": "telecom_customers_contract",
        "schema": [{
            "name": "telecom_customers", "physicalName": table,
            "properties": [{
                "name": "phone",
                "quality": [{"rule": "missingCount", "mustBe": 0}],
            }],
            "quality": [{"rule": "rowCount", "mustBeBetween": [100, 100000]}],
        }],
    }


# ── Step 1: subcontract storage ─────────────────────────────────────────────

def test_subcontract_written_to_type_bucket(sub_store, backend):
    sub = sub_store.create_from_payload(
        kind="pii", schema_table="telecom.customers", run_id="run_1",
        payload=_pii_payload(), summary_stats={"pii_confirmed": 1})
    # Object exists in pii bucket, not quality bucket
    assert backend.exists("pii-contracts", "telecom.customers/run_1.yaml")
    assert not backend.exists("quality-contracts", "telecom.customers/run_1.yaml")
    fetched = sub_store.get("pii", "telecom.customers", "run_1")
    assert fetched.subcontract_id == sub.subcontract_id
    assert fetched.payload["schema"][0]["properties"][0]["name"] == "phone"


def test_list_runs_accumulates_history(sub_store):
    for i in range(3):
        sub_store.create_from_payload(kind="quality", schema_table="t.t",
                                      run_id=f"run_{i}", payload=_quality_payload())
    runs = sub_store.list_runs("quality", "t.t")
    assert len(runs) == 3
    # index reflects them too
    tables = sub_store.list_tables("quality")
    assert "t.t" in tables


def test_edit_run_appends_edit_and_sets_reviewed(sub_store):
    sub_store.create_from_payload(kind="pii", schema_table="t.t", run_id="r",
                                  payload=_pii_payload())
    from redibis.store.subcontract_store import EditRecord
    updated = sub_store.update_payload("pii", "t.t", "r", {"changed": True},
                                       edit=EditRecord(field="payload", note="fix"))
    assert updated.status == STATUS_REVIEWED
    assert len(updated.edits) == 1
    assert updated.payload == {"changed": True}


# ── Step 2: select-and-merge ─────────────────────────────────────────────────

def test_merge_run_folds_into_active(store, sub_store):
    merger = RunMerger(store, sub_store)
    sub_store.create_from_payload(kind="pii", schema_table="telecom.customers",
                                  run_id="run_1", payload=_pii_payload())
    result = merger.merge_run("pii", "telecom.customers", "run_1", validate=False)
    assert result.upsert.is_new
    active = store.get_active("telecom.customers")
    from tests.contract_helpers import col_pii_engine
    assert col_pii_engine(active["schema"][0]["properties"][0]).get("detected") is True
    # subcontract marked merged + stamped with contract uuid
    sub = sub_store.get("pii", "telecom.customers", "run_1")
    assert sub.status == STATUS_MERGED
    assert sub.contract_uuid == result.upsert.contract_uuid


def test_two_kinds_merge_into_one_active(store, sub_store):
    merger = RunMerger(store, sub_store)
    sub_store.create_from_payload(kind="pii", schema_table="telecom.customers",
                                  run_id="p1", payload=_pii_payload())
    sub_store.create_from_payload(kind="quality", schema_table="telecom.customers",
                                  run_id="q1", payload=_quality_payload())
    merger.merge_run("pii", "telecom.customers", "p1", validate=False)
    merger.merge_run("quality", "telecom.customers", "q1", validate=False)
    active = store.get_active("telecom.customers")
    phone = active["schema"][0]["properties"][0]
    from tests.contract_helpers import col_pii_engine
    assert col_pii_engine(phone).get("detected") is True       # from pii run
    assert phone["quality"]                        # from quality run
    assert active["schema"][0]["quality"]          # table-level quality


def test_discard_run_never_merges(store, sub_store):
    merger = RunMerger(store, sub_store)
    sub_store.create_from_payload(kind="pii", schema_table="t.t", run_id="bad",
                                  payload=_pii_payload())
    merger.discard_run("pii", "t.t", "bad")
    sub = sub_store.get("pii", "t.t", "bad")
    assert sub.status == STATUS_DISCARDED
    # merge_latest skips discarded
    assert merger.merge_latest("pii", "t.t", validate=False) is None


def test_merge_latest_picks_newest(store, sub_store):
    import time
    merger = RunMerger(store, sub_store)
    for i in range(2):
        sub_store.create_from_payload(kind="quality", schema_table="t.t",
                                      run_id=f"r{i}", payload=_quality_payload())
        time.sleep(0.01)
    res = merger.merge_latest("quality", "t.t", validate=False)
    assert res is not None
    assert res.run_id == "r1"


# ── Step 3: PIIColumnReport ──────────────────────────────────────────────────

def test_column_report_basis_and_rule():
    t = Thresholds(presidio_min=0.80, gliner_min=0.70)
    d = PIIDetection(column="email", detected=False, presidio_score=0.90,
                     gliner_score=0.60)
    decided = decide_pii(d, "independent", t)
    report = build_column_report(decided, thresholds=t, equation="independent")
    assert report.is_pii is True
    assert report.regex == 0.90
    assert report.ner == 0.60
    assert report.basis == "regex"            # regex passed its floor, ner didn't
    assert "regex >= 0.8" in report.decision_rule
    assert report.engine_state["regex"]["confident"] is True
    assert report.engine_state["ner"]["confident"] is False
    assert report.engine_state["llm"]["status"] == "not_run"
    # round trips
    assert PIIColumnReport.from_dict(report.to_dict()).basis == "regex"


def test_column_report_includes_phone_engine_state():
    t = Thresholds(phone_min=0.80)
    d = PIIDetection(
        column="contact_phone",
        detected=True,
        entity_type="PHONE_NUMBER",
        confidence=0.97,
        phone_score=0.97,
        phone_entity="PHONE_NUMBER",
        msisdn_valid_rate=0.97,
        phone_valid_rate=1.0,
        phone_mobile_rate=1.0,
        phone_regions={"EG": 12},
        engine_states={"phone": {"enabled": True, "ran": True, "status": "matched"}},
    )
    report = build_column_report(d, thresholds=t, equation="independent")

    assert report.phone == 0.97
    assert report.engine_state["phone"]["ran"] is True
    assert report.engine_state["phone"]["confident"] is True
    assert report.engine_state["phone"]["entity_type"] == "PHONE_NUMBER"
    assert report.engine_state["phone"]["valid_rate"] == 1.0
    assert PIIColumnReport.from_dict(report.to_dict()).engine_state["phone"]["regions"] == {"EG": 12}


def test_pii_block_is_verdict_only_with_compact_evidence():
    """Policy-only privacy on the contract; discovery evidence in metadata sidecar."""
    from redibis.pii.contract_writer import PIIContractWriter
    t = Thresholds()
    d = decide_pii(PIIDetection(column="phone", detected=False, presidio_score=0.95,
                                 entity_type="PHONE_NUMBER"),
                   "independent", t)
    writer = PIIContractWriter("telecom", "customers", thresholds=t, run_id="r1")
    writer.add_detection(d)
    contract = writer.build()
    prop = contract["schema"][0]["properties"][0]
    from tests.contract_helpers import col_masking_default

    assert prop.get("entity_type") == "PHONE_NUMBER"
    privacy = prop.get("privacy") or {}
    assert privacy.get("classification")
    assert privacy.get("masking_policy")
    assert "classification_engine" not in privacy
    assert "confidence" not in privacy

    telemetry = contract["_column_telemetry"]["phone"]
    assert telemetry["confidence"] == 0.95
    assert "regex" in telemetry.get("discovery_engines", [])
    assert "regex >= 0.8" in telemetry.get("decision_rule", "")

    assert col_masking_default(prop) in ("fpe", "fake", "hash")
    mp = privacy.get("masking_policy") or {}
    assert mp.get("role_overrides") or mp.get("roles")


def test_validate_strips_internal_audit_fields():
    """Operational telemetry is not part of the ODCS spec — slim contracts validate."""
    from redibis.store.merger import merge_two_contracts
    from redibis.store.contract_metadata import slim_contract
    partial = _quality_payload()
    merged = merge_two_contracts(None, partial, workflow="quality",
                                 run_id="r1", run_uuid="u1")
    merged["provenance"] = [{"workflow": "quality"}]
    merged["last_updated"] = "2026-01-01"
    slim = slim_contract(merged)
    assert "provenance" not in slim
    assert merged["contract_uuid"]
    res = ContractStore.validate(slim, strict=False)
    assert res["valid"], res["errors"]


# ── Step 4: purge ────────────────────────────────────────────────────────────

def test_purge_resets_identity(store, sub_store):
    merger = RunMerger(store, sub_store)
    sub_store.create_from_payload(kind="pii", schema_table="telecom.customers",
                                  run_id="run_1", payload=_pii_payload())
    r1 = merger.merge_run("pii", "telecom.customers", "run_1", validate=False)
    first_uuid = r1.upsert.contract_uuid

    purge_result = store.purge("telecom.customers")
    assert purge_result["purged"] is True
    assert store.get_active("telecom.customers") is None
    assert store.get_history("telecom.customers") == []

    # re-merge after purge → brand new uuid
    sub_store.create_from_payload(kind="pii", schema_table="telecom.customers",
                                  run_id="run_2", payload=_pii_payload())
    r2 = merger.merge_run("pii", "telecom.customers", "run_2", validate=False)
    assert r2.upsert.contract_uuid != first_uuid


def test_purge_clears_overlays_and_metadata(store, sub_store):
    merger = RunMerger(store, sub_store)
    sub_store.create_from_payload(kind="pii", schema_table="telecom.customers",
                                  run_id="run_1", payload=_pii_payload())
    merger.merge_run("pii", "telecom.customers", "run_1", validate=False)
    store.set_pii_decision("telecom.customers", "phone", "not_pii", decided_by="test")
    from redibis.store.quality_decisions import QualityDecision
    store.quality_decisions.set("telecom.customers", QualityDecision(
        rule_id="r1", status="suppressed", column="phone"))
    store.definition_decisions.patch_table(
        "telecom.customers", {"description": "x", "tags": ["a"]}, decided_by="test")
    store.metadata.set_pii_summary("telecom.customers", {"detected": 1})

    store.purge("telecom.customers")

    assert store.get_active("telecom.customers") is None
    assert store.pii_decisions.get("telecom.customers") == {}
    assert store.quality_decisions.get("telecom.customers") == {}
    assert store.definition_decisions.get("telecom.customers") == {"table": {}, "columns": {}}
    assert store.metadata.get_pii_summary("telecom.customers") is None


def test_purge_keeps_runs_for_remerge(store, sub_store):
    merger = RunMerger(store, sub_store)
    sub_store.create_from_payload(kind="pii", schema_table="t.t", run_id="good",
                                  payload=_pii_payload(table="t.t"))
    merger.merge_run("pii", "t.t", "good", validate=False)
    store.purge("t.t")
    # run object still in bucket
    assert sub_store.get("pii", "t.t", "good") is not None


# ── Step 5: rules round-trip ─────────────────────────────────────────────────

def test_extract_rules_table_and_column():
    contract = _quality_payload()
    rules = extract_rules(contract)
    types = {r.type for r in rules}
    assert "row_count" in types        # table-level
    assert "not_null" in types         # column-level (missingCount)
    not_null = next(r for r in rules if r.type == "not_null")
    assert not_null.column == "phone"


def test_odcs_to_ge_reverse_mapper():
    contract = _quality_payload()
    suite = odcs_to_ge(contract)
    exp_types = {e["expectation_type"] for e in suite["expectations"]}
    assert "expect_table_row_count_to_be_between" in exp_types
    assert "expect_column_values_to_not_be_null" in exp_types
    nn = next(e for e in suite["expectations"]
              if e["expectation_type"] == "expect_column_values_to_not_be_null")
    assert nn["kwargs"]["column"] == "phone"


def test_regenerate_ge_alias():
    contract = _quality_payload()
    suite = regenerate(contract, "ge")
    assert suite["expectations"]
