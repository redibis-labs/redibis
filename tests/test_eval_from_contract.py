"""`redibis eval from-contract` — contract as expected values, opt-in samples."""

from __future__ import annotations

import pytest

from redibis.evaluation import dataset_from_contract, normalize_tags, validate_table_dataset
from redibis.evaluation.from_contract import CONSENT_UNAVAILABLE, CONSENT_WITHHELD
from redibis.evaluation.schema import TableEvalError


def _contract():
    return {"schema": [{"physicalName": "telco.customers", "properties": [
        {"name": "customer_id", "logicalType": "string", "tags": ["id"]},
        {"name": "msisdn", "logicalType": "string", "entity_type": "PHONE_NUMBER",
         "tags": ["PII", "contact", "tmp-2024"],
         "privacy": {"classification": "restricted",
                     "classification_engine": {"detected": True,
                                               "entity_type": "PHONE_NUMBER"}}},
        {"name": "monthly_charge", "logicalType": "number", "tags": ["metric"]},
    ]}]}


VALUES = {
    "customer_id": ["C001", "C002"],
    "msisdn": ["01012345678", "01199887766"],
    "monthly_charge": ["120.5", "90"],
}


class _Consent:
    def __init__(self, approved): self._approved = set(approved)
    def is_approved(self, table, column): return column in self._approved


def test_contract_becomes_expected_values():
    ds = dataset_from_contract(_contract(), table_name="telco.customers")
    by_name = {c["name"]: c for c in ds["columns"]}
    assert by_name["msisdn"]["is_pii"] is True
    assert by_name["msisdn"]["entity_type"] == "PHONE_NUMBER"
    assert by_name["msisdn"]["privacy_classification"] == "restricted"
    assert by_name["customer_id"]["is_pii"] is False


def test_no_samples_by_default_and_dataset_stays_portable():
    ds = dataset_from_contract(_contract(), table_name="t", sample_values=VALUES)
    assert ds["residency"] == "portable"
    assert ds["schema_version"] == "1.0"
    assert "warning" not in ds
    assert all("samples" not in c for c in ds["columns"])


def test_tag_allowlist_keeps_only_agreed_tags():
    ds = dataset_from_contract(
        _contract(), table_name="t", tags_allowlist=["pii", "contact", "id", "metric"]
    )
    by_name = {c["name"]: c for c in ds["columns"]}
    assert by_name["msisdn"]["tags"] == ["pii", "contact"]
    assert ds["notes"]["tags_dropped"] == ["tmp-2024"]


def test_tag_allowlist_is_case_insensitive_and_normalizes_spelling():
    assert normalize_tags(["PII", "Contact"], allowlist=["pii", "contact"]) == ["pii", "contact"]
    assert normalize_tags(["pii", "PII"], allowlist=None) == ["pii"]


def test_omitting_the_allowlist_keeps_every_tag():
    ds = dataset_from_contract(_contract(), table_name="t")
    by_name = {c["name"]: c for c in ds["columns"]}
    assert by_name["msisdn"]["tags"] == ["PII", "contact", "tmp-2024"]


def test_shape_samples_carry_no_characters_or_digits():
    ds = dataset_from_contract(
        _contract(), table_name="t", sample_values=VALUES,
        sample_mode="shape", sample_count=10,
    )
    by_name = {c["name"]: c for c in ds["columns"]}
    shapes = by_name["msisdn"]["samples"]
    assert shapes
    for shape in shapes:
        assert "01012345678" not in shape
        assert not any(ch.isdigit() for ch in shape.split(":", 1)[1])
    assert ds["residency"] == "contains_shapes"
    assert ds["schema_version"] == "1.1"
    assert ds["warning"]


def test_raw_samples_require_recorded_consent_per_column():
    ds = dataset_from_contract(
        _contract(), table_name="t", sample_values=VALUES,
        sample_mode="raw", sample_count=10, consent=_Consent({"customer_id"}),
    )
    by_name = {c["name"]: c for c in ds["columns"]}
    assert by_name["customer_id"]["samples"] == ["C001", "C002"]
    assert by_name["msisdn"]["samples"] == []
    assert by_name["msisdn"]["samples_withheld"] == CONSENT_WITHHELD
    assert ds["residency"] == "contains_values"
    assert "NOT value-free" in ds["warning"]


def test_raw_samples_refused_when_there_is_no_consent_store():
    ds = dataset_from_contract(
        _contract(), table_name="t", sample_values=VALUES,
        sample_mode="raw", sample_count=10, consent=None,
    )
    assert all(c["samples"] == [] for c in ds["columns"])
    assert all(c["samples_withheld"] == CONSENT_UNAVAILABLE for c in ds["columns"])
    assert ds["residency"] == "portable"


def test_sample_count_is_capped_at_the_requested_number():
    values = {"customer_id": [f"C{i:03d}" for i in range(50)]}
    ds = dataset_from_contract(
        _contract(), table_name="t", sample_values=values,
        sample_mode="raw", sample_count=10, consent=_Consent({"customer_id"}),
    )
    by_name = {c["name"]: c for c in ds["columns"]}
    assert len(by_name["customer_id"]["samples"]) == 10


def test_pii_only_filters_columns():
    ds = dataset_from_contract(_contract(), table_name="t", pii_only=True)
    assert [c["name"] for c in ds["columns"]] == ["msisdn"]


def test_output_round_trips_through_the_dataset_validator():
    ds = dataset_from_contract(
        _contract(), table_name="t", sample_values=VALUES, sample_mode="shape",
        sample_count=10,
    )
    validated = validate_table_dataset(ds)
    assert validated["schema_version"] == "1.1"
    assert validated["residency"] == "contains_shapes"
    # samples survive validation rather than being silently dropped
    assert any(c["samples"] for c in validated["columns"])


def test_schema_1_0_datasets_still_load():
    ds = dataset_from_contract(_contract(), table_name="t")
    assert ds["schema_version"] == "1.0"
    assert validate_table_dataset(ds)["columns"]


def test_empty_contract_is_an_error():
    with pytest.raises(TableEvalError):
        dataset_from_contract({"schema": []}, table_name="t")


def test_bad_sample_mode_is_an_error():
    with pytest.raises(TableEvalError):
        dataset_from_contract(_contract(), table_name="t", sample_mode="plaintext")
