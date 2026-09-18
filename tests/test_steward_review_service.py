"""Steward Review read model — overview + column page."""

from __future__ import annotations

import tempfile

from redibis.store.contract_store import ContractStore
from redibis.store.generation_ledger import Generation
from redibis.store.storage_backend import LocalBackend
from redibis.services.steward_review_service import StewardReviewService


def _contract():
    return {
        "apiVersion": "v3.0.1",
        "kind": "DataContract",
        "name": "customers_contract",
        "database_name": "db",
        "table_name": "customers",
        "version": "1.0.0",
        "schema": [{
            "name": "customers",
            "description": "customers",
            "properties": [
                {"name": "id", "logicalType": "integer"},
                {"name": "msisdn", "logicalType": "string", "entity_type": "PHONE_NUMBER",
                 "classification": "pii_personal", "tags": ["pii"]},
            ],
        }],
    }


def test_overview_and_column_payload():
    with tempfile.TemporaryDirectory() as tmp:
        store = ContractStore(LocalBackend(tmp), "c")
        store.upsert(_contract(), table="db.customers", workflow="manual", run_id="r1")
        store.generation_ledger.append("db.customers", "msisdn", [
            Generation(field="pii", source="regex", value={"is_pii": True},
                       confidence=0.81, run_id="r1", ts="t"),
            Generation(field="pii", source="ner", value={"is_pii": False},
                       confidence=0.22, run_id="r1", ts="t"),
        ])
        svc = StewardReviewService(store)
        ov = svc.overview("db.customers")
        assert ov["stats"]["columns_total"] == 2
        assert ov["stats"]["engine_agreement"]["contested"] >= 1
        assert ov["guarantee"]["total"] == 2
        page = svc.column("db.customers", "msisdn", actor="ada")
        pii_gens = page["generations"]["pii"]
        sources = {g["source"] for g in pii_gens}
        assert {"regex", "ner"} <= sources
        assert page["agreement"]["pii"] == "contested"
        assert page["profile"]["samples"] is None
        ov_id = next(c for c in ov["columns"] if c["column"] == "id")
        assert ov_id["agreement"] == "no_evidence"
        assert ov["stats"]["engine_agreement"]["no_evidence"] >= 1


def test_agreement_is_no_evidence_when_no_engine_voted():
    with tempfile.TemporaryDirectory() as tmp:
        store = ContractStore(LocalBackend(tmp), "c")
        store.upsert(_contract(), table="db.customers", workflow="manual", run_id="r1")
        page = StewardReviewService(store).column("db.customers", "id", actor="ada")
        assert page["agreement"]["pii"] == "no_evidence"


def test_human_only_generation_is_no_evidence_not_unanimous():
    with tempfile.TemporaryDirectory() as tmp:
        store = ContractStore(LocalBackend(tmp), "c")
        store.upsert(_contract(), table="db.customers", workflow="manual", run_id="r1")
        store.generation_ledger.append("db.customers", "id", [
            Generation(field="pii", source="human", value={"is_pii": False},
                       confidence=1.0, run_id="steward", ts="t"),
        ])
        page = StewardReviewService(store).column("db.customers", "id", actor="ada")
        assert page["agreement"]["pii"] == "no_evidence"
        assert page["agreement"]["pii"] != "unanimous"


def test_stats_count_no_evidence_separately():
    with tempfile.TemporaryDirectory() as tmp:
        store = ContractStore(LocalBackend(tmp), "c")
        store.upsert(_contract(), table="db.customers", workflow="manual", run_id="r1")
        ov = StewardReviewService(store).overview("db.customers")
        agr = ov["stats"]["engine_agreement"]
        assert "no_evidence" in agr
        assert agr["no_evidence"] == 2
        assert agr["unanimous"] == 0


def test_engine_accept_rationales_hidden_when_no_evidence():
    from redibis.review.rationale import codes_for_choice

    assert "engine_correct" not in codes_for_choice("regex", agreement="no_evidence")
    with tempfile.TemporaryDirectory() as tmp:
        store = ContractStore(LocalBackend(tmp), "c")
        store.upsert(_contract(), table="db.customers", workflow="manual", run_id="r1")
        page = StewardReviewService(store).column("db.customers", "id", actor="ada")
        assert page["agreement"]["pii"] == "no_evidence"
        for src, codes in page["rationale_codes"].items():
            assert "engine_correct" not in codes, src


def test_empty_role_never_receives_samples():
    from redibis.memory.consent import SamplingConsentStore
    from redibis.store.profile_store import ProfileStore

    with tempfile.TemporaryDirectory() as tmp:
        backend = LocalBackend(tmp)
        store = ContractStore(backend, "c")
        store.upsert(_contract(), table="db.customers", workflow="manual", run_id="r1")
        consent = SamplingConsentStore(backend, "c")
        consent.set_approved("db.customers", "msisdn", approved=True, approved_by="ada")
        profiles = ProfileStore(backend, "c", consent=consent)
        profiles.write(
            "db.customers", "r1",
            profile_result={"columns": {"msisdn": {"null_rate": 0.0}}},
            samples={"msisdn": ["01012345678"]},
            consent=consent,
        )
        page = StewardReviewService(store, profiles=profiles).column(
            "db.customers", "msisdn", actor="cli", role="", include_samples=True,
        )
        assert page["profile"]["samples"] is None
        assert page["profile"]["samples_withheld"] == "role"
