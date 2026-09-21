"""Deep Enrich — candidate isolation, path diff, selective merge."""

from __future__ import annotations

import pytest

from redibis.services.deep_enrich_service import DeepEnrichService
from redibis.store.contract_store import ContractStore
from redibis.store.storage_backend import LocalBackend
from redibis.store.subcontract_store import KIND_DEEP_ENRICH, SubcontractStore
from redibis.synthesis.merge import PathVerdict, build_partial_from_verdicts, merge_accepted
from redibis.synthesis.path_diff import diff_contracts, summarize_diff


def _backend(tmp_path):
    return LocalBackend(str(tmp_path / "store"))


def _contract(table: str = "db.customers") -> dict:
    db, _, tbl = table.partition(".")
    return {
        "apiVersion": "v3.0.1",
        "kind": "DataContract",
        "id": f"{db}.{tbl}",
        "name": tbl,
        "version": "1.0.0",
        "status": "active",
        "database_name": db,
        "table_name": tbl,
        "contract_uuid": "11111111-1111-1111-1111-111111111111",
        "schema": [{
            "name": f"{db}_{tbl}",
            "physicalName": table,
            "properties": [
                {
                    "name": "email",
                    "logicalType": "string",
                    "physicalType": "string",
                    "description": "legacy email",
                    "tags": ["contact"],
                    "classification": "internal",
                },
                {
                    "name": "amount",
                    "logicalType": "number",
                    "physicalType": "double",
                },
            ],
        }],
    }


@pytest.fixture
def stores(tmp_path):
    be = _backend(tmp_path)
    contract = ContractStore(be, bucket="active-contracts")
    subs = SubcontractStore(be)
    return contract, subs


def test_path_diff_detects_column_and_sla_and_cost():
    active = _contract()
    candidate = {
        **active,
        "apiVersion": "v3.1.0",
        "status": "draft",
        "schema": [{
            "name": "db_customers",
            "physicalName": "db.customers",
            "properties": [
                {
                    "name": "email",
                    "logicalType": "string",
                    "description": "Customer email address",
                    "tags": ["contact", "pii"],
                    "classification": "pii.email",
                    "pii": {"detected": True, "entity_type": "EMAIL"},
                },
                {"name": "amount", "logicalType": "number", "physicalType": "double"},
            ],
        }],
        "slaProperties": [
            {"property": "frequency", "value": 1, "unit": "d"},
            {"property": "retention", "value": 90, "unit": "d"},
        ],
        "customProperties": [
            {"property": "cost", "value": "tier-2"},
        ],
    }
    items = diff_contracts(active, candidate)
    paths = {i["path"] for i in items}
    assert "schema.properties.email.definition" in paths
    assert "schema.properties.email.pii" in paths
    assert "slaProperties.frequency" in paths
    assert "slaProperties.retention" in paths
    assert "customProperties.cost" in paths
    freshness = [i for i in items if i.get("field") == "freshness"]
    assert freshness
    cost = [i for i in items if i.get("field") == "cost"]
    assert cost
    summary = summarize_diff(items)
    assert summary["total"] >= 4


def test_deep_enrich_run_persists_candidate_without_upsert(stores):
    contract, subs = stores
    table = "db.customers"
    contract.upsert(_contract(table), table=table, workflow="manual")
    before = contract.get_active(table)
    svc = DeepEnrichService(contract, subs)
    result = svc.run(table, analysis_mode="deterministic", created_by="test")
    assert result["run_id"]
    assert result["writes_active_contract"] is False
    assert result["status"] == "draft"
    after = contract.get_active(table)
    assert after["version"] == before["version"]
    assert after["contract_uuid"] == before["contract_uuid"]
    listed = svc.list_runs(table)
    assert len(listed) == 1
    assert listed[0]["run_id"] == result["run_id"]
    # subcontract lives in deep-enrich bucket
    sub = subs.get(KIND_DEEP_ENRICH, table, result["run_id"])
    assert sub is not None
    assert sub.payload.get("candidate")


def test_selective_merge_accepts_definition_and_cost(stores):
    contract, subs = stores
    table = "db.customers"
    contract.upsert(_contract(table), table=table, workflow="manual")
    active = contract.get_active(table)
    candidate = {
        **active,
        "status": "draft",
        "schema": [{
            "name": "db_customers",
            "physicalName": table,
            "properties": [
                {
                    "name": "email",
                    "logicalType": "string",
                    "description": "Customer email (steward approved)",
                    "tags": ["contact"],
                    "classification": "internal",
                },
                {"name": "amount", "logicalType": "number", "physicalType": "double"},
            ],
        }],
        "customProperties": [{"property": "cost", "value": "tier-2"}],
    }
    verdicts = [
        PathVerdict(
            path="schema.properties.email.definition",
            decision="accept",
            value="Customer email (steward approved)",
            column="email",
            field="definition",
        ),
        PathVerdict(
            path="customProperties.cost",
            decision="accept",
            value={"property": "cost", "value": "tier-2"},
            field="cost",
        ),
    ]
    result = merge_accepted(
        contract, table,
        active=active, candidate=candidate, verdicts=verdicts,
        decided_by="steward", run_id="merge1", validate=False,
    )
    assert result["ok"] is True
    assert "schema.properties.email.definition" in result["accepted_paths"]
    merged = contract.get_active(table)
    props = {
        p["name"]: p
        for s in (merged.get("schema") or [])
        for p in (s.get("properties") or [])
    }
    assert props["email"]["description"] == "Customer email (steward approved)"
    cps = {
        e["property"]: e
        for e in (merged.get("customProperties") or [])
        if isinstance(e, dict)
    }
    assert cps["cost"]["value"] == "tier-2"


def test_stale_base_rejects_merge(stores):
    contract, subs = stores
    table = "db.customers"
    contract.upsert(_contract(table), table=table, workflow="manual")
    svc = DeepEnrichService(contract, subs)
    result = svc.run(table, analysis_mode="deterministic")
    run_id = result["run_id"]
    # Bump active version so candidate is stale
    contract.upsert(
        {"description": "bumped"}, table=table, workflow="manual",
    )
    with pytest.raises(ValueError, match="stale"):
        svc.merge(
            table, run_id, actor="steward",
            verdicts=[{
                "path": "schema.properties.email.definition",
                "decision": "accept",
                "value": "x",
                "column": "email",
                "field": "definition",
            }],
            validate=False,
        )


def test_path_verdicts_and_preview(stores):
    contract, subs = stores
    table = "db.customers"
    contract.upsert(_contract(table), table=table, workflow="manual")
    svc = DeepEnrichService(contract, subs)
    run = svc.run(table, analysis_mode="deterministic")
    run_id = run["run_id"]
    updated = svc.set_path_verdicts(
        table, run_id,
        [{
            "path": "schema.properties.email.definition",
            "decision": "accept",
            "value": "updated def",
            "column": "email",
            "field": "definition",
        }],
        actor="steward",
    )
    assert updated["path_verdicts"]["schema.properties.email.definition"]["decision"] == "accept"
    preview = svc.preview_merge(table, run_id)
    assert "schema.properties.email.definition" in preview["accepted_paths"]


def test_build_partial_routes_pii_to_overlay():
    active = _contract()
    candidate = {
        **active,
        "schema": [{
            "name": "db_customers",
            "properties": [{
                "name": "email",
                "logicalType": "string",
                "pii": {"detected": True, "entity_type": "EMAIL"},
                "tags": ["pii"],
            }],
        }],
    }
    preview = build_partial_from_verdicts(
        active=active,
        candidate=candidate,
        verdicts=[{
            "path": "schema.properties.email.pii",
            "decision": "accept",
            "value": {"is_pii": True, "entity_type": "EMAIL"},
            "column": "email",
            "field": "pii",
        }],
    )
    assert preview.pii_overlay
    assert preview.pii_overlay[0]["status"] == "pii"
    assert "schema.properties.email.pii" in preview.accepted_paths
