"""Tests for build_approved_partials — basket → ODCS partial assembly."""

from redibis.services.session_service import (
    ApprovedProperty, ApprovedSet, ScanSession, build_approved_partials,
)


def _session_with_basket(items):
    s = ScanSession(session_id="s1", table_name="telecom.customers", data_path="/tmp/x.csv")
    s.approved = ApprovedSet(items=items)
    return s


def test_mixed_basket_produces_two_partials():
    session = _session_with_basket([
        ApprovedProperty(prop_id="p1", kind="pii", column="email",
                         payload={"name": "email", "logicalType": "string",
                                  "pii": {"entity_type": "EMAIL_ADDRESS"}}),
        ApprovedProperty(prop_id="q1", kind="quality", column="age",
                         payload={"rule": "rangeCheck", "mustBeBetween": [0, 120]}),
        ApprovedProperty(prop_id="q2", kind="quality", column="__table__",
                         payload={"rule": "rowCount", "mustBeBetween": [1, 1000000]}),
    ])
    partials = build_approved_partials(session)
    assert partials["pii"] is not None
    assert partials["quality"] is not None
    assert partials["pii"]["schema"][0]["physicalName"] == "telecom.customers"
    assert partials["quality"]["schema"][0]["physicalName"] == "telecom.customers"
    assert len(partials["pii"]["schema"][0]["properties"]) == 1
    assert partials["pii"]["schema"][0]["properties"][0]["name"] == "email"
    q_schema = partials["quality"]["schema"][0]
    assert q_schema["properties"][0]["name"] == "age"
    assert q_schema["quality"][0]["rule"] == "rowCount"


def test_merged_items_included_in_partials():
    """Full basket is emitted so incremental quality merges stay lossless."""
    session = _session_with_basket([
        ApprovedProperty(prop_id="p1", kind="pii", column="email", status="merged",
                         payload={"name": "email"}),
        ApprovedProperty(prop_id="p2", kind="pii", column="phone",
                         payload={"name": "phone"}),
    ])
    partials = build_approved_partials(session)
    names = {p["name"] for p in partials["pii"]["schema"][0]["properties"]}
    assert names == {"email", "phone"}


def test_two_quality_rules_same_column_grouped():
    session = _session_with_basket([
        ApprovedProperty(prop_id="q1", kind="quality", column="age",
                         payload={"rule": "rangeCheck", "mustBeBetween": [0, 120]}),
        ApprovedProperty(prop_id="q2", kind="quality", column="age",
                         payload={"rule": "regex", "pattern": "^[0-9]+$"}),
    ])
    partials = build_approved_partials(session)
    age_prop = partials["quality"]["schema"][0]["properties"][0]
    assert age_prop["name"] == "age"
    assert len(age_prop["quality"]) == 2
    rules = {r["rule"] for r in age_prop["quality"]}
    assert rules == {"rangeCheck", "regex"}


def test_empty_basket_returns_none_partials():
    session = _session_with_basket([])
    partials = build_approved_partials(session)
    assert partials["pii"] is None
    assert partials["quality"] is None
