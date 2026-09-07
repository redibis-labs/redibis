"""Tests for the quality rule decision overlay."""

import tempfile

import pytest

from redibis.store.contract_store import ContractStore
from redibis.store.storage_backend import LocalBackend
from redibis.store.quality_decisions import reconcile_quality_rules
from redibis.contracts.rules import stable_rule_id, extract_rules


def _contract_with_quality():
    rule = {
        "type": "not_null",
        "mustBe": 0,
        "severity": "P1",
    }
    return {
        "database_name": "db",
        "table_name": "customers",
        "schema": [{
            "name": "customers",
            "physicalName": "db.customers",
            "properties": [
                {"name": "email", "quality": [rule]},
            ],
        }],
    }


def test_reconcile_suppresses_rule():
    contract = _contract_with_quality()
    col = "email"
    rid = stable_rule_id(col, contract["schema"][0]["properties"][0]["quality"][0])
    changed = reconcile_quality_rules(contract, {rid: {"status": "suppressed"}})
    assert changed is True
    assert contract["schema"][0]["properties"][0].get("quality", []) == []


def test_overlay_wins_over_later_merge():
    with tempfile.TemporaryDirectory() as tmp:
        backend = LocalBackend(tmp)
        store = ContractStore(backend, "contracts")

        partial = _contract_with_quality()
        store.upsert(partial, table="db.customers", workflow="quality", run_id="r1")

        active = store.get_active("db.customers")
        col = "email"
        rid = stable_rule_id(col, active["schema"][0]["properties"][0]["quality"][0])
        store.suppress_quality_rule("db.customers", rid, column=col)

        # Re-scan re-introduces the same rule via merge
        store.upsert(partial, table="db.customers", workflow="quality", run_id="r2")

        active2 = store.get_active("db.customers")
        rules = extract_rules(active2)
        assert len(rules) == 0


def test_manual_rule_survives_rescan():
    with tempfile.TemporaryDirectory() as tmp:
        backend = LocalBackend(tmp)
        store = ContractStore(backend, "contracts")

        partial = _contract_with_quality()
        store.upsert(partial, table="db.customers", workflow="quality", run_id="r1")

        manual = {
            "type": "unique",
            "severity": "P0",
            "mustBe": 0,
        }
        store.add_manual_quality_rule(
            "db.customers", manual, column="email", rule_id="manual_email_unique",
        )

        active = store.get_active("db.customers")
        rules = extract_rules(active)
        assert any(r.rule_id == "manual_email_unique" for r in rules)

        # Re-scan without that rule — manual overlay keeps it
        store.upsert(partial, table="db.customers", workflow="quality", run_id="r2")
        active2 = store.get_active("db.customers")
        rules2 = extract_rules(active2)
        assert any(r.rule_id == "manual_email_unique" for r in rules2)
