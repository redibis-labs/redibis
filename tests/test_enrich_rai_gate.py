"""Enrichment RAI attestation + preflight tests."""

from __future__ import annotations

import json

import pytest

from redibis.config import RedibisConfig
from redibis.enrich.providers import EnrichmentProvider
from redibis.enrich.rai_gate import compute_enrich_rai_preflight
from redibis.enrich.service import EnrichmentService, redact_contract_for_external_llm
from redibis.store.contract_store import ContractStore
from redibis.store.storage_backend import LocalBackend
from redibis.telemetry.pii_scope import infer_contains_raw_pii


class TrackProvider(EnrichmentProvider):
    def __init__(self, response: dict, *, residency: str = "public"):
        super().__init__(model="test-model", residency=residency)
        self.name = "external"
        self._response = response
        self.calls = 0

    def complete(self, system_prompt, user_prompt, *, json_mode=True):
        self.calls += 1
        return json.dumps(self._response)


@pytest.fixture
def store(tmp_path):
    return ContractStore(LocalBackend(tmp_path / "s"), bucket="active-contracts")


def _seed_pii_contract(store):
    contract = {
        "apiVersion": "v3.0.1",
        "kind": "DataContract",
        "name": "telecom_customers_contract",
        "version": "1.0.0",
        "status": "active",
        "schema": [{
            "name": "telecom_customers",
            "physicalName": "telecom.customers",
            "properties": [
                {
                    "name": "email",
                    "logicalType": "string",
                    "tags": ["pii"],
                    "privacy": {
                        "classification_engine": {
                            "detected": True,
                            "entity_type": "EMAIL_ADDRESS",
                            "confidence": 0.99,
                        },
                    },
                },
            ],
        }],
    }
    store.upsert(contract, table="telecom.customers", workflow="manual")


def test_attested_masked_external_skips_contract_pii_block():
    contract = {
        "schema": [{
            "name": "telecom_customers",
            "physicalName": "telecom.customers",
            "properties": [{"name": "email", "tags": ["pii"]}],
        }],
    }
    raw, cols = infer_contains_raw_pii(
        contract=contract, table="telecom.customers", attested_masked_external=True,
    )
    assert raw is False
    assert cols == ["email"]


def test_enrich_attested_with_sample_allows_external(store):
    _seed_pii_contract(store)
    svc = EnrichmentService(store)
    svc.add_sample_data(
        "telecom.customers",
        "masked.csv",
        b"email,name\n***@example.com,User A\n",
    )
    provider = TrackProvider(
        {"columns": {"email": {"business": {"definition": "Email"}}}},
        residency="public",
    )
    cfg = RedibisConfig()
    cfg.rai.hard_block_external_pii = True

    result = svc.enrich(
        "telecom.customers",
        provider,
        redibis_config=cfg,
        external_masked_acknowledged=True,
    )

    assert provider.calls == 1
    assert result.enrichment_meta["prompt_redacted"] is True
    assert result.enrichment_meta["external_masked_acknowledged"] is True
    assert result.enrichment_meta["rai"]["contains_raw_pii"] is False


def test_enrich_attested_without_sample_rejected(store):
    _seed_pii_contract(store)
    svc = EnrichmentService(store)
    provider = TrackProvider({"columns": {}}, residency="public")
    cfg = RedibisConfig()

    with pytest.raises(ValueError, match="Upload masked sample data"):
        svc.enrich(
            "telecom.customers",
            provider,
            redibis_config=cfg,
            external_masked_acknowledged=True,
        )
    assert provider.calls == 0


def test_preflight_reports_block_before_ack(store):
    _seed_pii_contract(store)
    provider = TrackProvider({"columns": {}}, residency="public")
    cfg = RedibisConfig()
    cfg.rai.hard_block_external_pii = True
    cfg.rai.enforce = True
    active = store.get_active("telecom.customers")

    pre = compute_enrich_rai_preflight(
        contract=active,
        table="telecom.customers",
        provider=provider,
        redibis_config=cfg,
        sample_data_count=0,
        external_masked_acknowledged=False,
    )
    assert pre["would_block"] is True
    assert "email" in pre["pii_columns"]
    assert pre["residency"] == "public"


def test_redact_strips_pii_tags_from_prompt_contract():
    contract = {
        "schema": [{
            "properties": [{
                "name": "email",
                "tags": ["pii", "contact"],
                "classification": "pii_personal",
                "privacy": {"classification_engine": {"detected": True, "entity_type": "EMAIL_ADDRESS"}},
                "business": {"example_values": ["a@b.com"]},
            }],
        }],
    }
    redacted = redact_contract_for_external_llm(contract)
    prop = redacted["schema"][0]["properties"][0]
    assert "pii" not in (prop.get("tags") or [])
    assert "contact" in prop["tags"]
    assert prop["business"]["example_values"] == ["(redacted — use masked sample data section)"]


def test_demo_provider_enriches_offline(store):
    _seed_pii_contract(store)
    from redibis.enrich.providers import get_provider

    provider = get_provider("demo")
    svc = EnrichmentService(store)
    result = svc.enrich("telecom.customers", provider, redibis_config=RedibisConfig())

    assert result.valid
    assert result.enrichment_meta["provider"] == "demo"
    props = result.candidate["schema"][0]["properties"]
    email = next(p for p in props if p["name"] == "email")
    assert email.get("business", {}).get("definition")
    assert "demo" in (email.get("business", {}).get("tags") or [])


def test_cloud_auto_redact_allows_gemini_with_pii(store):
    _seed_pii_contract(store)
    cfg = RedibisConfig()
    cfg.rai.hard_block_external_pii = False

    from dataclasses import dataclass

    @dataclass
    class StubCloud(EnrichmentProvider):
        name: str = "gemini"
        residency: str = "public"
        calls: int = 0

        def complete(self, system_prompt, user_prompt, *, json_mode=True):
            self.calls += 1
            assert "redacted" in user_prompt.lower()
            return json.dumps({"columns": {"email": {"business": {"definition": "Email field"}}}})

    svc = EnrichmentService(store)
    stub = StubCloud()
    result = svc.enrich("telecom.customers", stub, redibis_config=cfg)

    assert stub.calls == 1
    assert result.enrichment_meta["cloud_provider"] is True
    assert result.enrichment_meta["prompt_redacted"] is True
    assert result.enrichment_meta["rai_attested"] is True


def test_bypass_rai_allows_external_with_pii(store):
    _seed_pii_contract(store)
    provider = TrackProvider(
        {"columns": {"email": {"business": {"definition": "Email"}}}},
        residency="public",
    )
    cfg = RedibisConfig()
    cfg.rai.hard_block_external_pii = True

    svc = EnrichmentService(store)
    result = svc.enrich(
        "telecom.customers",
        provider,
        redibis_config=cfg,
        bypass_rai=True,
    )
    assert provider.calls == 1
    assert result.enrichment_meta["bypass_rai"] is True
