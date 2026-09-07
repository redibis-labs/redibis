"""Tests for LLM enrichment overhaul — shared context, full evidence, output scrub."""

import copy
import json
import pytest

from redibis.contracts.privacy import scrub_pii_enrichment_output, strip_quality_from_pii_columns
from redibis.enrich.providers import EnrichmentProvider
from redibis.enrich.service import EnrichmentService, apply_enrichment, enrichment_service_for_store
from redibis.store.contract_store import ContractStore
from redibis.store.storage_backend import LocalBackend


class FakeProvider(EnrichmentProvider):
    def __init__(self, response: dict, *, name: str = "fake"):
        super().__init__(model="fake-1")
        self.name = name
        self._response = response

    def complete(self, system_prompt, user_prompt, *, json_mode=True):
        return json.dumps(self._response)


@pytest.fixture
def store(tmp_path):
    return ContractStore(LocalBackend(tmp_path / "s"), bucket="active-contracts")


def _seed_pii_contract(store):
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
                {
                    "name": "merchant_mobile",
                    "logicalType": "string",
                    "physicalType": "string",
                    "classification": "pii_personal",
                    "tags": ["pii", "gdpr_personal_data"],
                    "entity_type": "PHONE_NUMBER",
                    "privacy": {
                        "classification": "pii_personal",
                        "classification_engine": {
                            "entity_type": "PHONE_NUMBER",
                            "confidence": 0.97,
                            "decision_rule": "regex >= 0.80",
                        },
                    },
                    "quality": [{
                        "rule": "minValue",
                        "implementation": {
                            "expectation_type": "expect_column_min_to_be_between",
                            "kwargs": {"min_value": 1000310561},
                        },
                    }],
                },
                {"name": "city", "logicalType": "string"},
            ],
        }],
    }
    store.upsert(contract, table="telecom.merchants", workflow="manual")
    store.metadata.merge_column_telemetry("telecom.merchants", {
        "merchant_mobile": {
            "entity_type": "PHONE_NUMBER",
            "confidence": 0.97,
            "decision_rule": "regex >= 0.80",
            "regex_hits": [{
                "pattern_name": "msisdn_egypt_any",
                "entity_type": "PHONE_NUMBER",
                "score": 0.97,
                "match_rate": 0.96,
            }],
            "sample": ["01007746235", "01118889999"],
            "null_rate": 0.0,
            "unique_ratio": 0.99,
        },
    })
    return contract


def test_build_context_ships_full_column_evidence(store):
    _seed_pii_contract(store)
    svc = EnrichmentService(store)
    provider = FakeProvider({})
    ctx = svc.build_context("telecom.merchants", provider)
    assert len(ctx.column_context) == 2
    mobile = next(c for c in ctx.column_context if c["name"] == "merchant_mobile")
    assert mobile["deterministic_verdict"]["entity_type"] == "PHONE_NUMBER"
    assert mobile["engine_evidence"]["regex_hits"]
    assert mobile["profiling"]["null_rate"] == 0.0
    assert mobile["similar_columns"] == []
    assert mobile["sample"] == ["01007746235", "01118889999"]
    assert "merchant_mobile" in json.dumps(ctx.contract_for_prompt)
    assert "existing_description" in ctx.contract_for_prompt
    assert "table.description" in ctx.system_prompt
    assert "Also return table.description" in ctx.user_prompt
    assert "CLASSIFICATION PACK DIGEST" in ctx.system_prompt
    assert "tag_vocabulary" in ctx.pack_digest
    mobile_reasoning = mobile.get("deterministic_reasoning") or {}
    assert mobile_reasoning.get("result", {}).get("tags") == ["MSISDN", "PII"]
    assert mobile_reasoning.get("result", {}).get("security") == "Confidential"


def test_ui_and_agent_identical_prompts(store):
    _seed_pii_contract(store)
    provider = FakeProvider({})
    web_svc = enrichment_service_for_store(store)
    agent_svc = enrichment_service_for_store(store)
    web_ctx = web_svc.build_context("telecom.merchants", provider)
    agent_ctx = agent_svc.build_context("telecom.merchants", provider)
    assert web_ctx.system_prompt == agent_ctx.system_prompt
    assert web_ctx.contract_for_prompt == agent_ctx.contract_for_prompt


def test_enrich_strips_pii_quality_and_scrubs_examples(store):
    _seed_pii_contract(store)
    svc = EnrichmentService(store)
    provider = FakeProvider({
        "columns": {
            "merchant_mobile": {
                "business": {
                    "definition": "Mobile",
                    "example_values": ["01007746235"],
                },
            },
            "city": {"business": {"definition": "City name"}},
        },
    })
    result = svc.enrich("telecom.merchants", provider)
    assert result.auto_written
    active = store.get_active("telecom.merchants")
    mobile = next(p for p in active["schema"][0]["properties"] if p["name"] == "merchant_mobile")
    assert "quality" not in mobile
    assert mobile["business"]["example_values"] == ["+20 1# ### ####", "01##########"]
    assert "01007746235" not in str(mobile["business"]["example_values"])


def test_scrub_pii_enrichment_output():
    contract = {
        "schema": [{"properties": [{
            "name": "email",
            "classification": "pii_personal",
            "tags": ["pii"],
            "entity_type": "EMAIL_ADDRESS",
            "business": {"example_values": ["real@corp.com"]},
            "quality": [{"rule": "minValue", "implementation": {"expectation_type": "expect_column_min_to_be_between"}}],
        }]}],
    }
    scrubbed = scrub_pii_enrichment_output(contract)
    prop = contract["schema"][0]["properties"][0]
    assert "real@corp.com" not in prop["business"]["example_values"]
    assert "quality" not in prop
    assert scrubbed == ["email"]


def test_cloud_sample_policy_masks_raw(store):
    _seed_pii_contract(store)
    svc = EnrichmentService(store)
    provider = FakeProvider({}, name="gemini")
    ctx = svc.build_context("telecom.merchants", provider)
    assert ctx.sample_policy == "masked"
    mobile = next(c for c in ctx.column_context if c["name"] == "merchant_mobile")
    assert "sample" not in mobile or mobile.get("sample_policy") == "masked"


def test_scrub_pii_definition_and_synonyms():
    from redibis.contracts.privacy import scrub_pii_enrichment_output

    contract = {
        "schema": [{"properties": [{
            "name": "mobile",
            "classification": "pii_personal",
            "tags": ["pii"],
            "entity_type": "PHONE_NUMBER",
            "business": {
                "definition": "Contact at 01007746235 or admin@test.com",
                "synonyms": ["01007746235"],
                "example_values": ["01007746235"],
            },
        }]}],
    }
    scrub_pii_enrichment_output(contract)
    biz = contract["schema"][0]["properties"][0]["business"]
    assert "01007746235" not in biz["definition"]
    assert "admin@test.com" not in biz["definition"]
    assert "01007746235" not in biz["synonyms"][0]
    assert "01007746235" not in str(biz["example_values"])


def test_pii_reason_persisted_to_telemetry(store):
    _seed_pii_contract(store)
    svc = EnrichmentService(store)
    provider = FakeProvider({
        "columns": {
            "merchant_mobile": {
                "business": {"definition": "Mobile line"},
                "pii": {
                    "classification": "pii_personal",
                    "entity_type": "PHONE_NUMBER",
                    "reason": "Regex and profiling agree — confirmed MSISDN.",
                },
            },
            "city": {"business": {"definition": "City"}},
        },
    })
    svc.enrich("telecom.merchants", provider)
    tel = store.metadata.get_column_evidence("telecom.merchants", "merchant_mobile")
    assert "Regex and profiling agree" in tel.get("llm_pii_reason", "")


def test_cli_and_agent_write_identical_contract(tmp_path):
    def _make_store(suffix: str):
        return ContractStore(
            LocalBackend(tmp_path / suffix), bucket="active-contracts",
        )

    store_a = _make_store("a")
    store_b = _make_store("b")
    _seed_pii_contract(store_a)
    _seed_pii_contract(store_b)

    delta = {
        "columns": {
            "merchant_mobile": {"business": {"definition": "Merchant mobile"}},
            "city": {"business": {"definition": "City name"}},
        },
    }
    provider = FakeProvider(delta)

    from redibis.enrich.service import enrichment_service_for_store

    cli_result = enrichment_service_for_store(store_a).enrich(
        "telecom.merchants", provider, enriched_by="cli",
    )
    agent_result = enrichment_service_for_store(store_b).enrich(
        "telecom.merchants", provider, enriched_by="agent",
    )

    assert cli_result.auto_written and agent_result.auto_written
    cli_city = next(
        p for p in store_a.get_active("telecom.merchants")["schema"][0]["properties"]
        if p["name"] == "city"
    )
    agent_city = next(
        p for p in store_b.get_active("telecom.merchants")["schema"][0]["properties"]
        if p["name"] == "city"
    )
    assert cli_city["business"] == agent_city["business"]


def test_bundle_global_disables_similar_context(store):
    from redibis.enrich.context import assemble_enrichment_context

    _seed_pii_contract(store)
    svc = EnrichmentService(store)
    bundle = assemble_enrichment_context(
        svc, "telecom.merchants", similar_context_enabled=False,
    )
    assert bundle["global"]["similar_context"]["enabled"] is False
    mobile = next(c for c in bundle["columns"] if c["name"] == "merchant_mobile")
    assert mobile.get("similar_columns") == []
    assert "deterministic_verdict" in mobile


def test_assemble_bundle_matches_build_context_columns(store):
    from redibis.enrich.context import assemble_enrichment_context
    from redibis.enrich.providers import get_provider

    _seed_pii_contract(store)
    svc = EnrichmentService(store)
    provider = get_provider("demo")
    ctx = svc.build_context("telecom.merchants", provider)
    bundle = assemble_enrichment_context(svc, "telecom.merchants", provider_name="demo")
    assert bundle["contract_for_prompt"] == ctx.contract_for_prompt
    assert len(bundle["columns"]) == len(ctx.column_context)


def test_merge_candidate_strips_pii_quality(store):
    _seed_pii_contract(store)
    svc = EnrichmentService(store)
    candidate = store.get_active("telecom.merchants")
    candidate = copy.deepcopy(candidate)
    mobile = next(p for p in candidate["schema"][0]["properties"] if p["name"] == "merchant_mobile")
    mobile["business"] = {
        "definition": "Mobile",
        "example_values": ["01007746235"],
    }
    candidate["enrichment_meta"] = {"enriched_by": "legacy", "pii_changes": []}
    svc._write_candidate("telecom.merchants", candidate)

    result = svc.merge_candidate("telecom.merchants")
    assert result["merged"]
    active = store.get_active("telecom.merchants")
    mobile = next(p for p in active["schema"][0]["properties"] if p["name"] == "merchant_mobile")
    assert "quality" not in mobile
    assert "01007746235" not in str(mobile.get("business", {}).get("example_values"))

