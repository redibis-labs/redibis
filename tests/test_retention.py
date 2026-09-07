"""Tests for table-level retention (TTL): NA default, round-trip, merge safety."""

import pytest

from redibis.store.storage_backend import LocalBackend
from redibis.store.contract_store import ContractStore
from redibis.contracts.retention import (
    get_retention, set_retention, clear_retention, build_retention_partial,
    format_retention, NA,
)


@pytest.fixture
def store(tmp_path):
    return ContractStore(LocalBackend(tmp_path / "storage"), bucket="active-contracts")


def _seed(table="telecom.cdr"):
    return {
        "apiVersion": "v3.0.1", "kind": "DataContract",
        "name": "telecom_cdr_contract",
        "schema": [{
            "name": "telecom_cdr", "physicalName": table,
            "properties": [{"name": "a_party_msisdn", "logicalType": "string"}],
        }],
    }


# ── pure helpers ──────────────────────────────────────────────────────────

def test_get_retention_defaults_to_na_when_absent():
    r = get_retention({})
    assert r["value"] == NA and r["unit"] == NA and r["set"] is False


def test_set_and_get_round_trip():
    c = {}
    set_retention(c, 90, unit="d", driver="regulatory", element="event_timestamp")
    r = get_retention(c)
    assert r["value"] == 90 and r["unit"] == "d"
    assert r["driver"] == "regulatory" and r["element"] == "event_timestamp"
    assert r["set"] is True
    assert format_retention(c) == "90 d (driver=regulatory, from=event_timestamp)"


def test_set_na_forces_unit_na():
    c = {}
    set_retention(c, "NA", unit="d")
    r = get_retention(c)
    assert r["value"] == NA and r["unit"] == NA


def test_set_replaces_existing_entry_only():
    c = {"slaProperties": [{"property": "latency", "value": 1, "unit": "h"}]}
    set_retention(c, 30, unit="d")
    props = [e["property"] for e in c["slaProperties"]]
    assert props.count("retention") == 1
    assert "latency" in props  # untouched


def test_numeric_string_coerced_to_int():
    c = {}
    set_retention(c, "90", unit="d")
    assert get_retention(c)["value"] == 90


def test_clear_removes_entry():
    c = {}
    set_retention(c, 90, unit="d")
    clear_retention(c)
    assert get_retention(c)["set"] is False
    assert "slaProperties" not in c


# ── store integration (goes through upsert / merge) ────────────────────────

def test_store_set_retention_versions_and_persists(store):
    store.upsert(_seed(), table="telecom.cdr", workflow="ge")
    assert store.get_retention("telecom.cdr")["value"] == NA  # default

    result = store.set_retention("telecom.cdr", 90, unit="d", driver="regulatory")
    assert result.version_after == "1.0.1"
    r = store.get_retention("telecom.cdr")
    assert r["value"] == 90 and r["unit"] == "d" and r["driver"] == "regulatory"


def test_store_set_retention_preserves_schema(store):
    store.upsert(_seed(), table="telecom.cdr", workflow="ge")
    store.set_retention("telecom.cdr", 90, unit="d")
    active = store.get_active("telecom.cdr")
    cols = [p["name"] for p in active["schema"][0]["properties"]]
    assert "a_party_msisdn" in cols  # retention merge didn't clobber schema


def test_store_get_retention_missing_table_is_na(store):
    r = store.get_retention("does.not_exist")
    assert r["value"] == NA and r["set"] is False


def test_store_set_retention_requires_active_contract(store):
    with pytest.raises(ValueError):
        store.set_retention("missing.table", 90, unit="d")


def test_build_partial_carries_only_retention():
    partial = build_retention_partial("telecom.cdr", 90, unit="d",
                                      physical_name="telecom.cdr")
    assert get_retention(partial)["value"] == 90
    assert partial["database_name"] == "telecom" and partial["table_name"] == "cdr"
