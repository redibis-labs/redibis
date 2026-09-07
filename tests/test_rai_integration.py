"""End-to-end RAI integration through EnrichmentService."""

from __future__ import annotations

import json

import pytest

from redibis.config import RedibisConfig
from redibis.enrich.providers import EnrichmentProvider
from redibis.enrich.service import EnrichmentService
from redibis.store.contract_store import ContractStore
from redibis.store.storage_backend import LocalBackend


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
                        },
                    },
                },
            ],
        }],
    }
    store.upsert(contract, table="telecom.customers", workflow="manual")


def test_enrich_hard_blocks_external_pii_without_calling_provider(store):
    _seed_pii_contract(store)
    provider = TrackProvider(
        {"columns": {"email": {"business": {"definition": "Email"}}}},
        residency="public",
    )
    cfg = RedibisConfig()
    cfg.rai.hard_block_external_pii = True
    cfg.rai.enforce = False
    cfg.rai.mode = "report"

    svc = EnrichmentService(store)
    with pytest.raises(PermissionError, match="raw PII blocked"):
        svc.enrich("telecom.customers", provider, redibis_config=cfg)

    assert provider.calls == 0


def test_enrich_advisory_allows_external_pii_when_hard_block_disabled(store):
    _seed_pii_contract(store)
    provider = TrackProvider(
        {"columns": {"email": {"business": {"definition": "Email"}}}},
        residency="public",
    )
    cfg = RedibisConfig()
    cfg.rai.hard_block_external_pii = False
    cfg.rai.mode = "report"

    svc = EnrichmentService(store)
    result = svc.enrich("telecom.customers", provider, redibis_config=cfg)

    assert provider.calls == 1
    assert result.enrichment_meta["rai"]["advisory_count"] == 1
    assert result.enrichment_meta["rai"]["contains_raw_pii"] is True
    assert "email" in result.enrichment_meta["rai"]["pii_columns"]


def test_enrich_enforce_blocks_model_allowlist_violation(store):
    _seed_pii_contract(store)
    provider = TrackProvider(
        {"columns": {"email": {"business": {"definition": "Email"}}}},
        residency="local",
    )
    cfg = RedibisConfig()
    cfg.rai.enforce = True
    cfg.rai.allowed_models = ["approved-only"]

    svc = EnrichmentService(store)
    with pytest.raises(PermissionError, match="not in allow-list"):
        svc.enrich("telecom.customers", provider, redibis_config=cfg)

    assert provider.calls == 0
