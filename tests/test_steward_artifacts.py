"""Steward artifacts A0–A5 and ODCS / leak invariants."""

from __future__ import annotations

import json
import tempfile

import pytest

from redibis.store.contract_store import ContractStore
from redibis.store.generation_ledger import Generation
from redibis.store.review_store import ColumnReview
from redibis.store.storage_backend import LocalBackend
from redibis.services.steward_review_service import StewardReviewService
from redibis.training.portable_assert import assert_portable_row


def _contract():
    return {
        "apiVersion": "v3.0.1",
        "kind": "DataContract",
        "id": "customers-contract",
        "status": "active",
        "name": "customers_contract",
        "database_name": "db",
        "table_name": "customers",
        "version": "1.0.0",
        "schema": [{
            "name": "customers",
            "description": "customers",
            "properties": [
                {"name": "id", "logicalType": "integer"},
                {"name": "email", "logicalType": "string", "entity_type": "EMAIL_ADDRESS",
                 "classification": "pii_personal", "tags": ["pii"],
                 "description": "customer email",
                 "quality": [
                     {"type": "library", "rule": "expect_column_min_to_be_between",
                      "minValue": "a@b.com", "maxValue": "z@b.com"},
                 ]},
            ],
        }],
    }


def _ready(svc, table="db.customers"):
    for col in ("id", "email"):
        svc.store.generation_ledger.append(table, col, [
            Generation(field="pii", source="regex", value={"is_pii": col == "email"},
                       confidence=0.8, run_id="r1", ts="t"),
        ])
        svc.decide(table, col, "pii", {
            "decision": "accept" if col == "email" else "no_action",
            "chosen_source": "regex" if col == "email" else "human",
            "rationale_code": "engine_correct" if col == "email" else "deprecated_column",
            "value": {"is_pii": col == "email"},
        }, actor="ada")
    for item in ("name", "description", "owner"):
        svc.decide_table(table, item, {
            "decision": "no_action", "rationale_code": "deprecated_column",
        }, actor="ada")


def test_finalize_produces_a0_a5_same_digest():
    with tempfile.TemporaryDirectory() as tmp:
        store = ContractStore(LocalBackend(tmp), "c")
        store.upsert(_contract(), table="db.customers", workflow="manual", run_id="r1")
        svc = StewardReviewService(store)
        _ready(svc)
        result = svc.finalize("db.customers", actor="ada")
        assert result["ok"] is True
        digest = result["artifacts"]["review_digest"]
        listing = svc.list_artifacts("db.customers")
        assert listing["review_digest"] == digest
        a0, _, _ = svc.get_artifact("db.customers", "contract")
        a1, _, _ = svc.get_artifact("db.customers", "verdicts")
        a2, _, _ = svc.get_artifact("db.customers", "evidence")
        graph, _, _ = svc.get_artifact("db.customers", "graph")
        c0 = json.loads(a0)
        c1 = json.loads(a1)
        c2 = json.loads(a2)
        g = json.loads(graph)
        assert c0["x-redibis-review"]["review_digest"] == digest
        assert c1["review_digest"] == digest
        assert c2["review_digest"] == digest
        assert g["review_digest"] == digest
        blob = json.dumps(c2)
        assert "samples" not in blob.lower() or '"samples"' not in blob
        pytest.importorskip("open_data_contract_standard")
        from redibis.contracts.validate_odcs import validate_odcs_contract
        slim = {k: v for k, v in c0.items() if k != "x-redibis-review"}
        slim_result = validate_odcs_contract(slim, strict=True)
        assert slim_result["valid"], slim_result.get("errors")
        with_x = validate_odcs_contract(c0, strict=False)
        assert with_x["valid"], with_x.get("errors")


def test_a4_refuses_unconsented_spans():
    with tempfile.TemporaryDirectory() as tmp:
        store = ContractStore(LocalBackend(tmp), "c")
        store.upsert(_contract(), table="db.customers", workflow="manual", run_id="r1")
        svc = StewardReviewService(store)
        _ready(svc)
        result = svc.finalize("db.customers", actor="ada")
        prefix = result["artifacts"]["prefix"]
        man = store.backend.get_json("c", f"{prefix}/finetune/manifest.json")
        assert man["counts"]["spans"] == 0
        assert man["counts"]["spans_skipped_no_consent"] >= 0
        cols = store.backend.get_text("c", f"{prefix}/finetune/columns.jsonl") or ""
        for line in cols.splitlines():
            if line.strip():
                assert_portable_row(json.loads(line))


def test_a2_and_a3_contain_no_source_digits_after_direct_append():
    with tempfile.TemporaryDirectory() as tmp:
        store = ContractStore(LocalBackend(tmp), "c")
        store.upsert(_contract(), table="db.customers", workflow="manual", run_id="r1")
        store.generation_ledger.append("db.customers", "email", [Generation(
            field="pii", source="llm", value={"is_pii": True}, confidence=0.9,
            run_id="r-dirty", ts="t",
            detail={"reasoning": "msisdn 01012345678 is a phone"},
        )])
        svc = StewardReviewService(store)
        _ready(svc)
        result = svc.finalize("db.customers", actor="ada")
        assert result["ok"] is True
        a2, _, _ = svc.get_artifact("db.customers", "evidence")
        a3, _, _ = svc.get_artifact("db.customers", "llm_context")
        blob = a2.decode("utf-8") + a3.decode("utf-8")
        assert "01012345678" not in blob


def test_a0_strips_quality_from_pii_columns():
    with tempfile.TemporaryDirectory() as tmp:
        store = ContractStore(LocalBackend(tmp), "c")
        store.upsert(_contract(), table="db.customers", workflow="manual", run_id="r1")
        svc = StewardReviewService(store)
        _ready(svc)
        svc.finalize("db.customers", actor="ada")
        a0, _, _ = svc.get_artifact("db.customers", "contract")
        c0 = json.loads(a0)
        props = (c0.get("schema") or [{}])[0].get("properties") or []
        email = next(p for p in props if p.get("name") == "email")
        assert "quality" not in email
        assert "a@b.com" not in json.dumps(c0)
        active = store.get_active("db.customers")
        active_email = next(
            p for p in (active.get("schema") or [{}])[0].get("properties") or []
            if p.get("name") == "email"
        )
        assert active_email.get("quality"), "active contract must keep quality"


def test_a1_a2_scrub_generation_value():
    with tempfile.TemporaryDirectory() as tmp:
        store = ContractStore(LocalBackend(tmp), "c")
        store.upsert(_contract(), table="db.customers", workflow="manual", run_id="r1")
        store.generation_ledger.append("db.customers", "email", [Generation(
            field="definition", source="llm",
            value={"text": "customer email a@b.com nid 29501011234567"},
            confidence=0.9, run_id="r-val", ts="t",
        )])
        svc = StewardReviewService(store)
        svc.decide("db.customers", "email", "definition", {
            "decision": "accept", "chosen_source": "llm",
            "rationale_code": "engine_correct",
            "value": "email a@b.com for nid 29501011234567",
        }, actor="ada")
        _ready(svc)
        svc.finalize("db.customers", actor="ada")
        a1, _, _ = svc.get_artifact("db.customers", "verdicts")
        a2, _, _ = svc.get_artifact("db.customers", "evidence")
        blob = a1.decode("utf-8") + a2.decode("utf-8")
        assert "29501011234567" not in blob
        assert "a@b.com" not in blob


def test_blocked_finalize_sets_guarantee_header(tmp_path, monkeypatch):
    backend = LocalBackend(tmp_path / "storage")
    store = ContractStore(backend, bucket="active-contracts")
    store.upsert(_contract(), table="db.customers", workflow="manual", run_id="r1")
    from redibis.webapp import backend as web
    monkeypatch.setattr(web, "get_contract_store", lambda: store)
    from fastapi.testclient import TestClient
    client = TestClient(web.app)
    r = client.post("/api/contracts/db.customers/steward/finalize")
    assert r.status_code == 200, r.text
    assert r.headers.get("X-Redibis-Guarantee") == "blocked"
    assert r.json().get("guaranteed") is False
    assert r.json().get("ok") is False


def test_cli_finalize_exits_one_when_blocked(tmp_path, monkeypatch):
    store = ContractStore(LocalBackend(tmp_path), "c")
    store.upsert(_contract(), table="db.customers", workflow="manual", run_id="r1")
    svc = StewardReviewService(store)
    monkeypatch.setattr(
        "redibis.cli.steward_cmd._svc", lambda args: (svc, store),
    )
    from argparse import Namespace
    from redibis.cli.steward_cmd import run_steward
    code = run_steward(Namespace(
        steward_action="finalize", table="db.customers", actor="ada", out_dir=None,
    ))
    assert code == 1
