"""Tests for the PII decision overlay: strip / add PII on contract columns."""

import tempfile

import pytest

from redibis.store.contract_store import ContractStore
from redibis.store.storage_backend import LocalBackend
from redibis.store.pii_decisions import (
    strip_pii_from_column,
    apply_pii_to_column,
    reconcile_pii_columns,
    recompute_pii_summary,
)


# ── Pure helpers ─────────────────────────────────────────────────────────────

def test_strip_pii_from_column_removes_all_signals():
    col = {
        "name": "phone",
        "logicalType": "string",
        "classification": "pii_personal",
        "tags": ["pii", "gdpr_personal_data", "billing"],
        "pii": {"detected": True, "entity_type": "PHONE_NUMBER"},
        "maskingPolicy": {"default": "fpe"},
        "description": "Customer phone",
        "customProperties": [
            {"property": "pii", "value": {"detected": True}},
            {"property": "owner", "value": "team-x"},
        ],
    }
    assert strip_pii_from_column(col) is True
    assert "pii" not in col
    assert "maskingPolicy" not in col
    assert "classification" not in col
    # non-PII tag survives, PII tags gone
    assert col["tags"] == ["billing"]
    # non-PII custom property survives
    assert col["customProperties"] == [{"property": "owner", "value": "team-x"}]
    # business fields untouched
    assert col["description"] == "Customer phone"
    assert col["logicalType"] == "string"


def test_strip_keeps_business_classification():
    col = {"name": "x", "classification": "confidential", "tags": ["pii"]}
    strip_pii_from_column(col)
    assert col["classification"] == "confidential"   # business value kept
    assert "tags" not in col                           # only tag was pii → removed


def test_strip_noop_on_clean_column():
    col = {"name": "city", "logicalType": "string", "tags": ["geo"]}
    assert strip_pii_from_column(col) is False
    assert col == {"name": "city", "logicalType": "string", "tags": ["geo"]}


def test_apply_pii_to_column():
    col = {"name": "ssn", "tags": ["sensitive"]}
    payload = {
        "classification": "pii_sensitive",
        "tags": ["pii", "gdpr_personal_data"],
        "pii": {"detected": True, "entity_type": "US_SSN"},
        "maskingPolicy": {"default": "hash"},
    }
    assert apply_pii_to_column(col, payload) is True
    assert col["privacy"]["classification"] == "pii_sensitive"
    assert col["entity_type"] == "US_SSN"
    assert col["classification"] == "pii_sensitive"
    assert set(col["tags"]) == {"sensitive", "pii", "gdpr_personal_data"}


def _contract_with_pii():
    return {
        "schema": [{
            "name": "telecom_customers",
            "physicalName": "telecom.customers",
            "properties": [
                {"name": "phone", "classification": "pii_personal",
                 "tags": ["pii", "gdpr_personal_data"],
                 "pii": {"detected": True, "entity_type": "PHONE_NUMBER"},
                 "maskingPolicy": {"default": "fpe"}},
                {"name": "city", "logicalType": "string"},
            ],
        }],
        "pii_summary": {"scan_run_id": "r1", "equation_used": "independent",
                        "pii_confirmed": 1, "pii_columns": ["phone"]},
    }


def test_reconcile_strips_then_recompute_summary():
    contract = _contract_with_pii()
    changed = reconcile_pii_columns(contract, {"phone": {"status": "not_pii"}})
    assert changed == ["phone"]
    phone = contract["schema"][0]["properties"][0]
    assert "pii" not in phone and "maskingPolicy" not in phone
    from redibis.store.pii_decisions import compute_pii_summary
    summary = compute_pii_summary(contract, preserve={"scan_run_id": "r1"})
    assert summary["pii_confirmed"] == 0
    assert summary["pii_columns"] == []
    assert summary["scan_run_id"] == "r1"


# ── ContractStore integration ───────────────────────────────────────────────

@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as td:
        yield ContractStore(LocalBackend(td), bucket="contracts")


def _seed_pii_contract(store):
    store.upsert(
        partial={"schema": [{
            "name": "telecom_customers", "physicalName": "telecom.customers",
            "properties": [
                {"name": "phone", "classification": "pii_personal",
                 "tags": ["pii", "gdpr_personal_data"],
                 "pii": {"detected": True, "entity_type": "PHONE_NUMBER"},
                 "maskingPolicy": {"default": "fpe"}},
                {"name": "city", "logicalType": "string"},
            ],
        }]},
        table="telecom.customers", workflow="pii", run_id="seed")


def test_strip_pii_via_store(store):
    _seed_pii_contract(store)
    res = store.set_pii_decision("telecom.customers", "phone", "not_pii",
                                 decided_by="test")
    assert res.version_after
    from redibis.contracts.privacy import column_is_pii
    phone = store.get_active("telecom.customers")["schema"][0]["properties"][0]
    assert not column_is_pii(phone)
    assert "privacy" not in phone
    assert "pii" not in phone
    assert "classification" not in phone
    assert "pii" not in (phone.get("tags") or [])


def test_overlay_wins_over_later_merge(store):
    """A PII upsert AFTER a strip must not re-PII the column (overlay wins)."""
    _seed_pii_contract(store)
    store.set_pii_decision("telecom.customers", "phone", "not_pii", decided_by="test")

    # A later scan/merge tries to re-add PII to phone.
    store.upsert(
        partial={"schema": [{
            "name": "telecom_customers", "physicalName": "telecom.customers",
            "properties": [
                {"name": "phone", "classification": "pii_personal",
                 "tags": ["pii", "gdpr_personal_data"],
                 "pii": {"detected": True, "entity_type": "PHONE_NUMBER"}},
            ],
        }]},
        table="telecom.customers", workflow="pii", run_id="rescan")

    from redibis.contracts.privacy import column_is_pii
    phone = store.get_active("telecom.customers")["schema"][0]["properties"][0]
    assert not column_is_pii(phone), "overlay should keep phone demoted after re-merge"
    assert "pii" not in (phone.get("tags") or [])


def test_add_pii_to_unscanned_column(store):
    _seed_pii_contract(store)
    # Payload is a plain ODCS column fragment (same shape pii_row_to_fragment
    # emits); built inline here to avoid importing the GE-backed services pkg.
    frag = {
        "classification": "pii_personal",
        "tags": ["pii", "gdpr_personal_data"],
        "pii": {"detected": True, "entity_type": "LOCATION", "confidence": 0.9},
        "maskingPolicy": {"default": "fake"},
    }
    store.set_pii_decision("telecom.customers", "city", "pii",
                           entity_type="LOCATION", payload=frag, decided_by="test")
    from redibis.contracts.privacy import column_is_pii
    city = store.get_active("telecom.customers")["schema"][0]["properties"][1]
    assert column_is_pii(city)
    assert city.get("entity_type") == "LOCATION"
    assert city["privacy"]["classification"] == "pii_personal"
    assert "pii" in (city.get("tags") or [])


def test_strip_unknown_column_raises(store):
    _seed_pii_contract(store)
    with pytest.raises(ValueError):
        store.set_pii_decision("telecom.customers", "nope", "not_pii")


def test_strip_without_contract_raises(store):
    with pytest.raises(ValueError):
        store.set_pii_decision("ghost.table", "x", "not_pii")
