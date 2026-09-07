"""Tests for ODCS Pydantic compatibility (strip redibis-only fields)."""

import pytest

from redibis.contracts.odcs_compat import prepare_for_odcs_pydantic, build_odcs_model
from redibis.contracts.exporter import export_contract
from redibis.store.contract_store import ContractStore


def _sample_contract():
    return {
        "contract_uuid": "b8354c83-35ae-4875-9345-47b6272a35e3",
        "database_name": "telecom",
        "table_name": "customers",
        "version": "1.0.0",
        "enrichment_meta": {"provider": "vllm"},
        "provenance": [{"workflow": "pii"}],
        "schema": [{
            "name": "customers",
            "physicalName": "telecom.customers",
            "properties": [{
                "name": "email",
                "logicalType": "string",
                "privacy": {"classification_engine": {"detected": True}},
                "pii": {"detected": True},
                "maskingPolicy": {"default": "hash"},
            }],
        }],
    }


def test_prepare_strips_identity_and_extensions():
    clean = prepare_for_odcs_pydantic(_sample_contract())
    assert "contract_uuid" not in clean
    assert "database_name" not in clean
    assert "enrichment_meta" in clean  # no longer silently stripped — validate must reject
    prop = clean["schema"][0]["properties"][0]
    assert "privacy" not in prop
    assert "pii" not in prop
    assert "maskingPolicy" not in prop


def test_enrichment_meta_rejected_by_odcs_validate():
    result = ContractStore.validate(_sample_contract(), strict=False)
    assert not result["valid"]
    assert any("enrichment_meta" in e.lower() or "extra" in e.lower() for e in result["errors"])


@pytest.mark.skipif(
    not pytest.importorskip("open_data_contract_standard", reason="ODCS not installed"),
    reason="requires open-data-contract-standard",
)
def test_build_odcs_model_accepts_redibis_contract():
    clean = prepare_for_odcs_pydantic(_sample_contract())
    clean.pop("enrichment_meta", None)
    build_odcs_model(clean)


@pytest.mark.skipif(
    not pytest.importorskip("datacontract", reason="datacontract-cli not installed"),
    reason="requires datacontract-cli",
)
def test_exporter_accepts_contract_uuid():
    sample = _sample_contract()
    sample.pop("enrichment_meta", None)
    out = export_contract(sample, "odcs")
    assert "email" in out or "customers" in out


def test_contract_store_validate_accepts_contract_uuid():
    sample = _sample_contract()
    sample.pop("enrichment_meta", None)
    result = ContractStore.validate(sample, strict=True)
    assert result["valid"], result["errors"]
