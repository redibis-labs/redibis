"""Contract spec vs operational metadata separation."""

import tempfile

from redibis.contracts.privacy import build_privacy_block, column_is_pii
from redibis.store.contract_store import ContractStore
from redibis.store.storage_backend import LocalBackend


def test_active_contract_has_no_embedded_telemetry():
    with tempfile.TemporaryDirectory() as td:
        store = ContractStore(LocalBackend(td), bucket="contracts")
        privacy = build_privacy_block(
            classification="pii_personal",
            masking_policy={"default": "fpe", "reversible": True},
        )
        store.upsert(
            partial={
                "schema": [{
                    "name": "db_t", "physicalName": "db.t",
                    "properties": [
                        {"name": "phone", "logicalType": "string",
                         "classification": "pii_personal",
                         "entity_type": "PHONE_NUMBER",
                         "tags": ["pii"], "privacy": privacy},
                    ],
                }],
                "_scan_metadata": {"scan_run_id": "r1", "equation_used": "balanced"},
            },
            table="db.t",
            workflow="pii",
            run_id="r1",
        )
        active = store.get_active("db.t")
        assert "provenance" not in active
        assert "pii_summary" not in active
        assert "last_updated" not in active
        assert column_is_pii(active["schema"][0]["properties"][0])

        meta = store.get_metadata("db.t")
        assert len(meta["provenance"]) >= 1
        assert meta["pii_summary"]["pii_confirmed"] == 1
        assert meta["telemetry"]["last_updated_by_workflow"] == "pii"

        pkg = store.export_integration_package("db.t")
        assert pkg["spec"] is not None
        assert pkg["provenance"]
        assert pkg["pii_summary"]["scan_run_id"] == "r1"


def test_slim_contract_strips_legacy_embedded_fields():
    from redibis.store.contract_metadata import slim_contract

    raw = {
        "version": "1.0.0",
        "provenance": [{"workflow": "pii"}],
        "pii_summary": {"pii_confirmed": 1},
        "last_updated": "2026-01-01",
        "schema": [{"properties": [
            {"name": "x", "pii": {"detected": True},
             "maskingPolicy": {"default": "hash"}},
        ]}],
    }
    slim = slim_contract(raw)
    assert "provenance" not in slim
    assert slim["schema"][0]["properties"][0].get("privacy")
    assert "pii" not in slim["schema"][0]["properties"][0]


def test_slim_contract_migrates_legacy_owner_to_team():
    """Legacy owner: mdaoor must become ODCS team and pass strict validation."""
    from redibis.store.contract_metadata import slim_contract
    from redibis.store.contract_store import ContractStore

    raw = {
        "apiVersion": "v3.0.1",
        "kind": "DataContract",
        "id": "00000000-0000-0000-0000-000000000001",
        "name": "data_realistic_eshop_customer_account_contract",
        "version": "1.0.43",
        "status": "active",
        "owner": "mdaoor",
        "team": [
            {"name": "mdaoor", "username": "mdaoor", "role": "owner"},
        ],
        "schema": [{
            "name": "data_realistic_eshop_customer_account",
            "physicalName": "data.realistic_eshop_customer_account",
            "owner": "mdaoor",
            "team": [
                {"name": "mdaoor", "username": "mdaoor", "role": "owner"},
            ],
            "properties": [
                {"name": "email_address", "logicalType": "string"},
            ],
        }],
    }
    slim = slim_contract(raw)
    assert "owner" not in slim
    assert "owner" not in slim["schema"][0]
    assert "team" not in slim["schema"][0]
    assert slim["team"] == [
        {"name": "mdaoor", "username": "mdaoor", "role": "owner"},
    ]
    result = ContractStore.validate(slim, strict=True)
    assert result["valid"], result["errors"]


def test_upsert_repairs_legacy_owner_on_existing_contract():
    """Next schema upsert rewrites a stored owner string into team via slim_contract."""
    with tempfile.TemporaryDirectory() as td:
        store = ContractStore(LocalBackend(td), bucket="contracts")
        store.backend.put_yaml(
            "contracts",
            "active/data.realistic_eshop_customer_account.yaml",
            {
                "apiVersion": "v3.0.1",
                "kind": "DataContract",
                "id": "00000000-0000-0000-0000-000000000002",
                "name": "data_realistic_eshop_customer_account_contract",
                "version": "1.0.0",
                "status": "active",
                "owner": "mdaoor",
                "database_name": "data",
                "table_name": "realistic_eshop_customer_account",
                "schema": [{
                    "name": "data_realistic_eshop_customer_account",
                    "physicalName": "data.realistic_eshop_customer_account",
                    "owner": "mdaoor",
                    "properties": [
                        {"name": "email_address", "logicalType": "string"},
                    ],
                }],
            },
        )
        store.upsert(
            partial={
                "apiVersion": "v3.0.1",
                "kind": "DataContract",
                "schema": [{
                    "name": "data_realistic_eshop_customer_account",
                    "physicalName": "data.realistic_eshop_customer_account",
                    "properties": [
                        {"name": "email_address", "logicalType": "string"},
                        {"name": "full_name", "logicalType": "string"},
                    ],
                }],
            },
            table="data.realistic_eshop_customer_account",
            workflow="schema",
            run_id="schema_repair",
            validate=True,
        )
        active = store.get_active("data.realistic_eshop_customer_account")
        assert "owner" not in active
        assert "owner" not in active["schema"][0]
        assert active["team"][0]["username"] == "mdaoor"
        assert active["team"][0]["role"] == "owner"
        assert {p["name"] for p in active["schema"][0]["properties"]} >= {
            "email_address", "full_name",
        }
