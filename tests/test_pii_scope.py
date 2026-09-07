"""Tests for structural PII scope detection."""

from __future__ import annotations

from redibis.telemetry.pii_scope import (
    column_has_pii_markers,
    contract_columns_with_pii,
    infer_contains_raw_pii,
    payload_may_contain_raw_pii,
)

TABLE = "telecom.customers"


def test_column_has_pii_from_privacy_block():
    prop = {
        "name": "email",
        "privacy": {"classification_engine": {"detected": True}},
    }
    assert column_has_pii_markers(prop)


def test_contract_columns_with_pii():
    contract = {
        "schema": [{
            "name": "telecom_customers",
            "physicalName": TABLE,
            "properties": [
                {"name": "email", "tags": ["pii"]},
                {"name": "city"},
            ],
        }],
    }
    cols = contract_columns_with_pii(contract, TABLE)
    assert cols == ["email"]


def test_infer_from_payload_when_contract_clean():
    contains, cols = infer_contains_raw_pii(
        contract={"schema": [{"name": "t", "physicalName": TABLE, "properties": []}]},
        table=TABLE,
        user_prompt="contact alice@example.com for details",
    )
    assert contains is True
    assert cols == []


def test_payload_masked_sample_not_flagged():
    assert payload_may_contain_raw_pii(
        "MASKED SAMPLE DATA — do not treat as raw PII\nname,email\n***,***"
    ) is False
