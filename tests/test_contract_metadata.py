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
