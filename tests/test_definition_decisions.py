"""Tests for definition/tag decision overlay (authoritative tag replacement)."""

import tempfile

from redibis.store.contract_store import ContractStore
from redibis.store.storage_backend import LocalBackend
from redibis.store.definition_decisions import reconcile_definition_decisions


def _contract_with_tags():
    return {
        "database_name": "db",
        "table_name": "customers",
        "schema": [{
            "name": "customers",
            "physicalName": "db.customers",
            "tags": ["finance", "legacy"],
            "properties": [
                {"name": "email", "tags": ["contact", "pii"], "description": "Email"},
            ],
        }],
    }


def test_reconcile_replaces_tags_not_union():
    contract = _contract_with_tags()
    decisions = {
        "table": {"tags": ["governed"]},
        "columns": {"email": {"tags": ["contact"]}},
    }
    assert reconcile_definition_decisions(contract, decisions) is True
    assert contract["schema"][0]["tags"] == ["governed"]
    assert contract["schema"][0]["properties"][0]["tags"] == ["contact"]


def test_definition_overlay_wins_on_merge():
    with tempfile.TemporaryDirectory() as tmp:
        backend = LocalBackend(tmp)
        store = ContractStore(backend, "contracts")

        partial = _contract_with_tags()
        store.upsert(partial, table="db.customers", workflow="import", run_id="r1")

        store.patch_definitions(
            "db.customers",
            table_patch={"tags": ["stewarded"]},
            column_patches={"email": {"tags": []}},
        )

        # Merge tries to union tags back in
        rescan = _contract_with_tags()
        rescan["schema"][0]["tags"] = ["finance", "legacy", "new_scan_tag"]
        rescan["schema"][0]["properties"][0]["tags"] = ["contact", "pii", "new_col_tag"]
        store.upsert(rescan, table="db.customers", workflow="import", run_id="r2")

        active = store.get_active("db.customers")
        assert active["schema"][0]["tags"] == ["stewarded"]
        assert active["schema"][0]["properties"][0]["tags"] == []
