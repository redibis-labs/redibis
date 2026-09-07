"""Tests for merge_approved — basket upsert via ContractStore."""

from redibis.services.session_service import (
    ApprovedProperty, ApprovedSet, ScanSession, merge_approved,
)
from redibis.store.contract_store import ContractStore
from redibis.store.storage_backend import LocalBackend
from tests.contract_helpers import col_pii_engine


def test_merge_approved_upserts_and_flags_items(tmp_path):
    backend = LocalBackend(tmp_path / "storage")
    store = ContractStore(backend, bucket="contracts")
    session = ScanSession(session_id="s1", table_name="telecom.customers", data_path="/tmp/x.csv")
    session.approved = ApprovedSet(items=[
        ApprovedProperty(prop_id="p1", kind="pii", column="email",
                         payload={"name": "email", "logicalType": "string",
                                  "tags": ["pii"], "pii": {"entity_type": "EMAIL_ADDRESS"}}),
        ApprovedProperty(prop_id="q1", kind="quality", column="age",
                         payload={"rule": "rangeCheck", "mustBeBetween": [0, 120]}),
    ])
    result = merge_approved(session, store, validate=False)
    assert result["merged_version"]
    assert "pii" in result["results"]
    assert "quality" in result["results"]
    for p in session.approved.items:
        assert p.status == "merged"
        assert p.merged_version == result["merged_version"]
    active = store.get_active("telecom.customers")
    assert active is not None
    props = active["schema"][0]["properties"]
    assert any(pr["name"] == "email" for pr in props)
    age_prop = next(pr for pr in props if pr["name"] == "age")
    assert age_prop["quality"][0]["rule"] == "rangeCheck"


def test_incremental_quality_merge_preserves_both_rules(tmp_path):
    backend = LocalBackend(tmp_path / "storage")
    store = ContractStore(backend, bucket="contracts")
    session = ScanSession(session_id="s1", table_name="telecom.customers", data_path="/tmp/x.csv")
    session.approved = ApprovedSet(items=[
        ApprovedProperty(prop_id="q1", kind="quality", column="age",
                         payload={"rule": "validValues", "validValues": ["a", "b"]}),
    ])
    merge_approved(session, store, validate=False)
    session.approved.add(ApprovedProperty(
        prop_id="q2", kind="quality", column="age",
        payload={"rule": "regex", "pattern": "^[0-9]+$"}))
    merge_approved(session, store, validate=False)
    active = store.get_active("telecom.customers")
    age_prop = next(pr for pr in active["schema"][0]["properties"] if pr["name"] == "age")
    rules = {r["rule"] for r in age_prop["quality"]}
    assert rules == {"validValues", "regex"}


def test_merge_twice_preserves_contract_content(tmp_path):
    backend = LocalBackend(tmp_path / "storage")
    store = ContractStore(backend, bucket="contracts")
    session = ScanSession(session_id="s1", table_name="telecom.customers", data_path="/tmp/x.csv")
    session.approved = ApprovedSet(items=[
        ApprovedProperty(prop_id="p1", kind="pii", column="email",
                         payload={"name": "email", "logicalType": "string",
                                  "tags": ["pii"], "pii": {"entity_type": "EMAIL_ADDRESS"}}),
    ])
    merge_approved(session, store, validate=False)
    after_first = store.get_active("telecom.customers")
    merge_approved(session, store, validate=False)
    after_second = store.get_active("telecom.customers")
    email_first = next(pr for pr in after_first["schema"][0]["properties"] if pr["name"] == "email")
    email_second = next(pr for pr in after_second["schema"][0]["properties"] if pr["name"] == "email")
    assert col_pii_engine(email_first) == col_pii_engine(email_second)
    assert email_first["tags"] == email_second["tags"]


def test_merge_empty_basket_noop(tmp_path):
    backend = LocalBackend(tmp_path / "storage")
    store = ContractStore(backend, bucket="contracts")
    session = ScanSession(session_id="s1", table_name="telecom.customers", data_path="/tmp/x.csv")
    result = merge_approved(session, store, validate=False)
    assert result["noop"] is True
    assert result["merged_version"] is None
    assert result["results"] == {}
