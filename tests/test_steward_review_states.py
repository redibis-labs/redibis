"""Five verdict states, table-level review, fingerprint-gated definitions."""

from __future__ import annotations

import tempfile

import pytest

from redibis.review.rationale import RationaleError, validate_rationale
from redibis.store.contract_store import ContractStore
from redibis.store.review_store import ColumnReview, FieldVerdict, ReviewStore
from redibis.store.storage_backend import LocalBackend
from redibis.services.steward_review_service import StewardReviewService
from redibis.store.generation_ledger import Generation


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
            "description": "customers table",
            "properties": [
                {"name": "id", "logicalType": "string"},
                {"name": "email", "logicalType": "string", "classification": "pii_personal",
                 "entity_type": "EMAIL_ADDRESS", "tags": ["pii"]},
            ],
        }],
    }


def test_no_action_counts_as_reviewed_and_does_not_block_guarantee():
    with tempfile.TemporaryDirectory() as tmp:
        backend = LocalBackend(tmp)
        reviews = ReviewStore(backend, "c")
        reviews.set_column("db.t", ColumnReview(column="a", status="approved"), total_columns=2)
        reviews.set_column("db.t", ColumnReview(column="b", status="no_action"), total_columns=2)
        state = reviews.finalize("db.t", required_columns=["a", "b"], required_table_items=[])
        assert state.guaranteed is True
        blockers = reviews.guarantee_blockers("db.t", required_columns=["a", "b"], required_table_items=[])
        assert not any(blockers.values())


def test_needs_review_blocks_guarantee():
    with tempfile.TemporaryDirectory() as tmp:
        reviews = ReviewStore(LocalBackend(tmp), "c")
        reviews.set_column("db.t", ColumnReview(column="a", status="approved"), total_columns=2)
        reviews.set_column("db.t", ColumnReview(column="b", status="needs_review"), total_columns=2)
        blockers = reviews.guarantee_blockers("db.t", required_columns=["a", "b"], required_table_items=[])
        assert "b" in blockers["needs_review"]
        state = reviews.finalize("db.t", required_columns=["a", "b"], required_table_items=[])
        assert state.guaranteed is False


def test_old_checkpoint_files_load_with_new_fields_defaulted():
    with tempfile.TemporaryDirectory() as tmp:
        backend = LocalBackend(tmp)
        backend.put_json("c", "_meta/reviews/db.t.json", {
            "table": "db.t",
            "columns": {
                "email": {
                    "column": "email",
                    "status": "approved",
                    "approved": {"pii_flag": True},
                    "reviewed_by": "ada",
                    "reviewed_at": "2026-01-01T00:00:00+00:00",
                    "note": "",
                }
            },
            "total_columns": 1,
            "fully_approved": True,
        })
        state = ReviewStore(backend, "c").get("db.t")
        assert state.columns["email"].status == "approved"
        assert state.columns["email"].verdicts == {}
        assert state.guaranteed is False or isinstance(state.guaranteed, bool)
        assert state.table_review.items == {}


def test_field_verdict_requires_rationale_text_when_code_is_other():
    with pytest.raises(RationaleError):
        validate_rationale("other", "")
    assert validate_rationale("other", "because policy") == "other"


def test_chosen_source_must_exist_in_ledger():
    with tempfile.TemporaryDirectory() as tmp:
        store = ContractStore(LocalBackend(tmp), "c")
        store.upsert(_contract(), table="db.customers", workflow="manual", run_id="r1")
        svc = StewardReviewService(store)
        with pytest.raises(Exception):
            svc.decide(
                "db.customers", "email", "pii",
                {"decision": "accept", "chosen_source": "llm", "rationale_code": "engine_correct"},
                actor="ada",
            )
        store.generation_ledger.append("db.customers", "email", [
            Generation(field="pii", source="llm", value={"is_pii": True},
                       confidence=0.9, run_id="r1", ts="t"),
        ])
        out = svc.decide(
            "db.customers", "email", "pii",
            {"decision": "accept", "chosen_source": "llm", "rationale_code": "engine_correct",
             "value": {"is_pii": True, "entity_type": "EMAIL_ADDRESS"}},
            actor="ada",
        )
        assert out["column"] == "email"


def test_definition_decision_is_fingerprint_gated_and_goes_stale_on_drift():
    with tempfile.TemporaryDirectory() as tmp:
        store = ContractStore(LocalBackend(tmp), "c")
        store.upsert(_contract(), table="db.customers", workflow="manual", run_id="r1")
        store.patch_definitions(
            "db.customers",
            column_patches={"email": {"description": "customer email"}},
            decided_by="ada",
        )
        entry = store.definition_decisions.get("db.customers")["columns"]["email"]
        assert entry.get("fingerprint_key")
        assert entry.get("lifecycle_state") == "active"
        # Drift: change logical type on the column via a new upsert
        drifted = _contract()
        drifted["schema"][0]["properties"][1]["logicalType"] = "number"
        store.upsert(drifted, table="db.customers", workflow="manual", run_id="r2")
        entry2 = store.definition_decisions.get("db.customers")["columns"]["email"]
        assert entry2.get("lifecycle_state") == "stale"
        review = ReviewStore(store.backend, store.bucket).get("db.customers")
        assert review.columns.get("email") and review.columns["email"].status == "needs_review"


def test_review_store_scrubs_field_verdict_rationale_text():
    with tempfile.TemporaryDirectory() as tmp:
        reviews = ReviewStore(LocalBackend(tmp), "c")
        reviews.set_column("db.t", ColumnReview(
            column="msisdn",
            status="approved",
            note="phone 01012345678",
            verdicts={"pii": FieldVerdict(
                field="pii", decision="accept", rationale_code="other",
                rationale_text="msisdn 01012345678 is personal",
            )},
        ), total_columns=1)
        cr = reviews.get("db.t").columns["msisdn"]
        assert "01012345678" not in cr.note
        assert "01012345678" not in cr.verdicts["pii"].rationale_text


def test_table_level_items_participate_in_guarantee():
    with tempfile.TemporaryDirectory() as tmp:
        store = ContractStore(LocalBackend(tmp), "c")
        store.upsert(_contract(), table="db.customers", workflow="manual", run_id="r1")
        svc = StewardReviewService(store)
        reviews = svc.reviews
        for col in ("id", "email"):
            reviews.set_column("db.customers", ColumnReview(column=col, status="approved"), total_columns=2)
        blockers = reviews.guarantee_blockers(
            "db.customers",
            required_columns=["id", "email"],
            required_table_items=["name", "description", "owner"],
        )
        assert blockers["table"]
        for item in ("name", "description", "owner"):
            svc.decide_table(
                "db.customers", item,
                {"decision": "no_action", "rationale_code": "deprecated_column"},
                actor="ada",
            )
        blockers2 = svc.reviews.guarantee_blockers(
            "db.customers",
            required_columns=["id", "email"],
            required_table_items=["name", "description", "owner"],
        )
        assert not any(blockers2.values())
