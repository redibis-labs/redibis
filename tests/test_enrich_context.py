"""Tests for enrichment context bundle (§5A)."""

import json
import pytest

from redibis.enrich.context import (
    assemble_enrichment_context,
    contract_from_bundle,
    load_context_draft,
    preview_enrichment_prompt,
    save_context_draft,
)
from redibis.enrich.providers import EnrichmentProvider
from redibis.enrich.service import EnrichmentService
from redibis.store.contract_store import ContractStore
from redibis.store.storage_backend import LocalBackend


class FakeProvider(EnrichmentProvider):
    def __init__(self, response: dict):
        super().__init__(model="fake-1")
        self.name = "fake"
        self._response = response

    def complete(self, system_prompt, user_prompt, *, json_mode=True):
        return json.dumps(self._response)


@pytest.fixture
def store(tmp_path):
    return ContractStore(LocalBackend(tmp_path / "s"), bucket="active-contracts")


def _seed(store):
    contract = {
        "apiVersion": "v3.0.1", "kind": "DataContract",
        "name": "telecom_customers_contract", "version": "1.0.0", "status": "active",
        "schema": [{
            "name": "telecom_customers", "physicalName": "telecom.customers",
            "properties": [
                {"name": "email", "logicalType": "string", "tags": ["pii"],
                 "classification": "pii_personal",
                 "privacy": {"classification_engine": {"entity_type": "EMAIL"}}},
                {"name": "city", "logicalType": "string"},
            ],
        }],
    }
    store.upsert(contract, table="telecom.customers", workflow="manual")
    return contract


def test_assemble_context_includes_columns_and_instructions(store):
    _seed(store)
    svc = EnrichmentService(store)
    bundle = assemble_enrichment_context(svc, "telecom.customers")
    assert bundle["table"] == "telecom.customers"
    assert len(bundle["columns"]) == 2
    assert bundle["instructions"]["system_prompt"]
    assert "email" in {c["name"] for c in bundle["columns"]}


def test_context_draft_roundtrip_and_preview(store):
    _seed(store)
    svc = EnrichmentService(store)
    bundle = assemble_enrichment_context(svc, "telecom.customers")
    bundle["instructions"]["extra_instructions"] = "Use UK English."
    city = next(c for c in bundle["columns"] if c["name"] == "city")
    city["business"] = {"definition": "Edited city definition"}
    save_context_draft(store, "telecom.customers", bundle)
    loaded = load_context_draft(store, "telecom.customers")
    assert loaded["instructions"]["extra_instructions"] == "Use UK English."
    preview = preview_enrichment_prompt(svc, loaded)
    assert "Use UK English." in preview["system_prompt"]
    assert "Edited city definition" in preview["user_prompt"] or "city" in preview["user_prompt"]


def test_contract_from_bundle_applies_column_edits(store):
    _seed(store)
    svc = EnrichmentService(store)
    bundle = assemble_enrichment_context(svc, "telecom.customers")
    email = next(c for c in bundle["columns"] if c["name"] == "email")
    email["classification"] = "none"
    email["entity_type"] = None
    contract = contract_from_bundle(bundle)
    email_prop = next(
        p for p in contract["schema"][0]["properties"] if p["name"] == "email"
    )
    assert email_prop.get("classification") == "none"


def test_enrich_with_contract_file_without_active_store(store, tmp_path):
    contract = {
        "apiVersion": "v3.0.1",
        "kind": "DataContract",
        "name": "merchants_contract",
        "version": "1.0.0",
        "status": "active",
        "schema": [{
            "name": "merchants",
            "physicalName": "telecom.merchants",
            "properties": [
                {"name": "city", "logicalType": "string", "physicalType": "string"},
            ],
        }],
    }
    path = tmp_path / "telecom.merchants.yaml"
    import yaml
    path.write_text(yaml.dump(contract), encoding="utf-8")

    from redibis.enrich.context import load_contract_for_enrichment

    loaded, table = load_contract_for_enrichment(path)
    assert table == "telecom.merchants"
    assert store.get_active(table) is None

    svc = EnrichmentService(store)
    provider = FakeProvider({"columns": {"city": {"business": {"definition": "City name"}}}})
    result = svc.enrich(table, provider, input_contract=loaded)
    assert result.auto_written
    active = store.get_active(table)
    city = next(p for p in active["schema"][0]["properties"] if p["name"] == "city")
    assert city["business"]["definition"] == "City name"


def test_build_context_uses_input_contract(store):
    contract = {
        "apiVersion": "v3.0.1",
        "kind": "DataContract",
        "schema": [{
            "physicalName": "telecom.offline",
            "properties": [{"name": "id", "logicalType": "string"}],
        }],
    }
    svc = EnrichmentService(store)
    ctx = svc.build_context(
        "telecom.offline", FakeProvider({}), input_contract=contract,
    )
    assert ctx.c_det["schema"][0]["physicalName"] == "telecom.offline"
    assert len(ctx.column_context) == 1


def test_enrich_with_context_bundle_auto_writes(store):
    _seed(store)
    svc = EnrichmentService(store)
    bundle = assemble_enrichment_context(svc, "telecom.customers")
    bundle["instructions"]["extra_instructions"] = "Define every column."
    provider = FakeProvider({
        "columns": {
            "email": {"business": {"definition": "Contact email"}},
            "city": {"business": {"definition": "City name"}},
        },
    })
    from redibis.store.run_output_writer import RunOutputWriter

    writer = RunOutputWriter(
        backend=store.backend, bucket="pii-reports",
        workflow="enrich", table="telecom.customers", run_id=bundle["run_id"],
    )
    result = svc.enrich(
        "telecom.customers", provider, context_bundle=bundle, run_writer=writer,
    )
    assert result.auto_written
    active = store.get_active("telecom.customers")
    city = next(p for p in active["schema"][0]["properties"] if p["name"] == "city")
    assert city["business"]["definition"] == "City name"
    assert load_context_draft(store, "telecom.customers") is None
