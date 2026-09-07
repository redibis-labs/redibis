"""Tests for ApprovedProperty / ApprovedSet basket semantics."""

from redibis.services.session_service import ApprovedProperty, ApprovedSet


def test_add_dedupes_same_pii_column():
    basket = ApprovedSet()
    p1 = ApprovedProperty(prop_id="ap_pii_aaa", kind="pii", column="email",
                          label="EMAIL", payload={"name": "email", "pii": {}})
    p2 = ApprovedProperty(prop_id="ap_pii_bbb", kind="pii", column="email",
                          label="EMAIL_UPDATED", payload={"name": "email", "pii": {"v": 2}})
    basket.add(p1)
    out = basket.add(p2)
    assert len(basket.items) == 1
    assert out.prop_id == "ap_pii_aaa"
    assert basket.items[0].label == "EMAIL_UPDATED"
    assert basket.items[0].updated_at is not None


def test_update_remove_clear():
    basket = ApprovedSet()
    p = ApprovedProperty(prop_id="ap_q_1", kind="quality", column="age",
                         payload={"rule": "rangeCheck"})
    basket.add(p)
    assert basket.update("ap_q_1", note="reviewed").note == "reviewed"
    assert basket.remove("ap_q_1")
    assert len(basket.items) == 0

    basket.add(ApprovedProperty(prop_id="m1", kind="pii", column="a", status="merged",
                                payload={"name": "a"}))
    basket.add(ApprovedProperty(prop_id="a1", kind="pii", column="b", status="approved",
                                payload={"name": "b"}))
    assert basket.clear(only_merged=True) == 1
    assert len(basket.items) == 1
    assert basket.clear() == 1
    assert len(basket.items) == 0


def test_round_trip_to_dict():
    basket = ApprovedSet(items=[
        ApprovedProperty(prop_id="x", kind="pii", column="phone", label="PHONE",
                         payload={"name": "phone"}),
    ])
    restored = ApprovedSet.from_dict(basket.to_dict())
    assert restored.summary() == basket.summary()
    assert restored.items[0].column == "phone"
    assert restored.items[0].payload["name"] == "phone"


def test_quality_ge_rules_dedupe_by_expectation_type():
    basket = ApprovedSet()
    ge_a = {"engine": "greatExpectations",
            "implementation": {"expectation_type": "expect_column_values_to_not_be_null", "kwargs": {}}}
    ge_b = {"engine": "greatExpectations",
            "implementation": {"expectation_type": "expect_column_values_to_be_unique", "kwargs": {}}}
    basket.add(ApprovedProperty(prop_id="a", kind="quality", column="age", payload=ge_a))
    basket.add(ApprovedProperty(prop_id="b", kind="quality", column="age", payload=ge_b))
    assert len(basket.items) == 2


def test_two_native_rules_same_column_coexist():
    basket = ApprovedSet()
    basket.add(ApprovedProperty(prop_id="a", kind="quality", column="age",
                                payload={"rule": "missingCount", "mustBe": 0}))
    basket.add(ApprovedProperty(prop_id="b", kind="quality", column="age",
                                payload={"rule": "regex", "pattern": "^[0-9]+$"}))
    assert len(basket.items) == 2
