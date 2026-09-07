"""Acceptance tests for enrich PII-quality leak fix + verification seams."""

from __future__ import annotations

import json

import pytest

from redibis.contracts.privacy import (
    column_is_pii,
    quality_rule_is_value_bearing,
    scrub_pii_enrichment_output,
)
from redibis.enrich.providers import EnrichmentProvider
from redibis.enrich.service import EnrichmentService
from redibis.store.contract_store import ContractStore
from redibis.store.storage_backend import LocalBackend


class FakeProvider(EnrichmentProvider):
    def __init__(self, response: dict | None = None, *, name: str = "fake"):
        super().__init__(model="fake-1")
        self.name = name
        self._response = response or {}

    def complete(self, system_prompt, user_prompt, *, json_mode=True):
        return json.dumps(self._response)


PII_COLUMNS = frozenset({
    "merchant_mobile",
    "merchant_contact_name",
    "merchant_email",
    "tax_registration_number",
    "unified_national_number",
    "settlement_iban",
    "business_address",
})


def _value_bearing_quality(min_value: int | str) -> list[dict]:
    return [{
        "rule": "minValue",
        "implementation": {
            "expectation_type": "expect_column_min_to_be_between",
            "kwargs": {"min_value": min_value},
        },
    }]


def _merchant_registry_contract() -> dict:
    """Fixture mirroring the leaked contract.llm.yaml PII columns."""
    columns = [
        {
            "name": "merchant_id",
            "logicalType": "string",
            "classification": "restricted",
            "business": {"example_values": ["M-10001", "M-10002"]},
        },
        {
            "name": "merchant_mobile",
            "logicalType": "string",
            "classification": "pii_personal",
            "tags": ["pii"],
            "privacy": {
                "classification": "pii_personal",
                "classification_engine": {
                    "entity_type": "PHONE_NUMBER",
                    "confidence": 0.97,
                    "decision_rule": "regex >= 0.80",
                },
            },
            "quality": _value_bearing_quality(1000310561),
        },
        {
            "name": "merchant_contact_name",
            "logicalType": "string",
            "classification": "pii_personal",
            "tags": ["pii"],
            "privacy": {
                "classification_engine": {"entity_type": "PERSON", "confidence": 0.9},
            },
            "quality": _value_bearing_quality("Ahmed Hassan"),
        },
        {
            "name": "merchant_email",
            "logicalType": "string",
            "classification": "pii_personal",
            "tags": ["pii"],
            "privacy": {
                "classification_engine": {"entity_type": "EMAIL_ADDRESS", "confidence": 0.95},
            },
            "quality": _value_bearing_quality("merchant@example.com"),
        },
        {
            "name": "tax_registration_number",
            "logicalType": "string",
            "classification": "pii_sensitive",
            "tags": ["pii"],
            "privacy": {
                "classification_engine": {"entity_type": "EG_NATIONAL_ID", "confidence": 0.99},
            },
            "quality": _value_bearing_quality(29001011234567),
        },
        {
            "name": "unified_national_number",
            "logicalType": "string",
            "classification": "pii_sensitive",
            "tags": ["pii"],
            "privacy": {
                "classification_engine": {"entity_type": "EG_NATIONAL_ID", "confidence": 0.99},
            },
            "quality": _value_bearing_quality(70012345678901),
        },
        {
            "name": "settlement_iban",
            "logicalType": "string",
            "classification": "pii_sensitive",
            "tags": ["pii"],
            "privacy": {
                "classification_engine": {"entity_type": "IBAN_CODE", "confidence": 0.98},
            },
            "quality": _value_bearing_quality("EG38001900050000000026318000"),
        },
        {
            "name": "business_address",
            "logicalType": "string",
            "classification": "pii_personal",
            "tags": ["pii"],
            "privacy": {
                "classification_engine": {"entity_type": "LOCATION", "confidence": 0.85},
            },
            "quality": _value_bearing_quality("123 Nile St"),
        },
        {
            "name": "city",
            "logicalType": "string",
            "business": {"example_values": ["Cairo", "Alexandria"]},
        },
    ]
    return {
        "apiVersion": "v3.0.1",
        "kind": "DataContract",
        "name": "merchant_registry",
        "version": "1.0.0",
        "status": "active",
        "schema": [{
            "name": "merchants",
            "physicalName": "telecom.merchant_registry",
            "properties": columns,
        }],
    }


@pytest.fixture
def store(tmp_path):
    return ContractStore(LocalBackend(tmp_path / "s"), bucket="active-contracts")


def _assert_no_value_bearing_quality(prop: dict) -> None:
    quality = prop.get("quality")
    if not quality:
        return
    for rule in quality:
        assert not quality_rule_is_value_bearing(rule), (
            f"{prop.get('name')}: value-bearing quality leaked: {rule}"
        )


def test_enrich_strips_quality_from_all_pii_columns(store):
    contract = _merchant_registry_contract()
    store.upsert(contract, table="telecom.merchant_registry", workflow="manual")

    delta = {
        "columns": {
            col["name"]: {
                "business": {
                    "definition": f"Definition for {col['name']}",
                    **({"example_values": col["business"]["example_values"]}
                       if col.get("business", {}).get("example_values") else {}),
                },
            }
            for col in contract["schema"][0]["properties"]
        },
    }
    svc = EnrichmentService(store)
    result = svc.enrich("telecom.merchant_registry", FakeProvider(delta))
    assert result.auto_written

    active = store.get_active("telecom.merchant_registry")
    for prop in active["schema"][0]["properties"]:
        name = prop.get("name")
        if name in PII_COLUMNS or column_is_pii(prop):
            assert "quality" not in prop, f"PII quality leak on {name}"
        if name == "merchant_id":
            assert prop["business"]["example_values"] == ["M-10001", "M-10002"]
        if name == "city":
            assert prop["business"]["example_values"] == ["Cairo", "Alexandria"]


def test_scrub_pii_enrichment_output_is_pii_gated():
    contract = {
        "schema": [{
            "properties": [
                {
                    "name": "city",
                    "business": {"example_values": ["Cairo", "Giza"]},
                },
                {
                    "name": "mobile",
                    "classification": "pii_personal",
                    "tags": ["pii"],
                    "privacy": {
                        "classification_engine": {"entity_type": "PHONE_NUMBER"},
                    },
                    "business": {
                        "definition": "Call 01007746235",
                        "example_values": ["01007746235"],
                    },
                },
            ],
        }],
    }
    scrub_pii_enrichment_output(contract)
    city, mobile = contract["schema"][0]["properties"]
    assert city["business"]["example_values"] == ["Cairo", "Giza"]
    assert "01007746235" not in str(mobile["business"]["example_values"])
    assert "01007746235" not in mobile["business"]["definition"]
    assert "quality" not in mobile


def test_cloud_prompt_preserves_deterministic_verdict_in_evidence(store):
    contract = _merchant_registry_contract()
    store.upsert(contract, table="telecom.merchant_registry", workflow="manual")
    store.metadata.merge_column_telemetry("telecom.merchant_registry", {
        "merchant_mobile": {
            "entity_type": "PHONE_NUMBER",
            "decision_rule": "regex >= 0.80",
            "regex_hits": [{
                "pattern_name": "msisdn_egypt_any",
                "match_rate": 0.96,
                "entity_type": "PHONE_NUMBER",
            }],
        },
    })

    svc = EnrichmentService(store)
    ctx = svc.build_context("telecom.merchant_registry", FakeProvider(name="gemini"))
    assert ctx.prompt_redacted is True

    mobile = next(c for c in ctx.column_context if c["name"] == "merchant_mobile")
    verdict = mobile["deterministic_verdict"]
    assert verdict["entity_type"] == "PHONE_NUMBER"
    assert verdict["classification"] == "pii_personal"
    assert verdict["decision_rule"] == "regex >= 0.80"
    assert "deterministic_reasoning" in mobile
    assert "PHONE_NUMBER" in ctx.user_prompt
    assert "pii_personal" in ctx.user_prompt


def test_agentic_enrich_uses_shared_service(store, monkeypatch):
    contract = _merchant_registry_contract()
    store.upsert(contract, table="telecom.merchant_registry", workflow="manual")

    calls: list[str] = []

    class TrackingSvc(EnrichmentService):
        def enrich(self, table, provider, **kwargs):
            calls.append("enrich")
            return super().enrich(table, provider, **kwargs)

    tracking = TrackingSvc(store)
    monkeypatch.setattr(
        "redibis.enrich.service.enrichment_service_for_store",
        lambda _store: tracking,
    )

    from redibis.agents.models import PipelineNode
    from redibis.agents.tool_runner import ToolContext, _run_enrich
    from redibis.config import RedibisConfig

    node = PipelineNode(kind="enrich", label="Enrich", params={"provider": "demo"})
    ctx = ToolContext(
        contract_store=store,
        config=RedibisConfig(),
        dry_run=False,
    )
    out = _run_enrich(node, "telecom.merchant_registry", ctx)
    assert calls == ["enrich"]
    assert out.get("valid") is True
