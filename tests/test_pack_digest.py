"""Tests for classification pack digest + deterministic reasoning trace."""

from __future__ import annotations

import pytest

from redibis.classification.pack_digest import build_classification_pack_digest, build_pack_digest
from redibis.classification.policy_pack import get_builtin_pack
from redibis.classification.reasoning import build_deterministic_reasoning
from redibis.enrich.service import EnrichmentService
from redibis.store.contract_store import ContractStore
from redibis.store.storage_backend import LocalBackend


@pytest.fixture
def policy():
    return get_builtin_pack("telecom")


def test_build_pack_digest_compact_shape(policy):
    digest = build_pack_digest(policy)
    assert digest["pack"] == "telecom"
    assert "PII" in digest["tag_vocabulary"]["DataSensitivity"]
    assert "MSISDN" in digest["tag_vocabulary"]["TelcoDataType"]
    assert "Confidential" in digest["tag_vocabulary"]["DataSecurity"]
    assert digest["entity_to_tag"]["PHONE_NUMBER"] == "TelcoDataType:MSISDN"
    assert digest["entity_to_tag"]["NATIONAL_ID"] == "DataSensitivity:SubscriberPII"
    assert "MSISDN" in digest["multi_classification"]
    assert "DataSensitivity:PII" in digest["multi_classification"]["MSISDN"]
    assert "DataSecurity:Confidential" in digest["multi_classification"]["SubscriberPII"]
    assert digest["security_derivation"]["PII"] == "Confidential"
    assert digest["security_derivation"]["BiometricData"] == "Restricted"
    assert "RegulatoryCompliance" not in digest["tag_vocabulary"]
    assert "role_routing" not in digest
    assert "jurisdiction_cotags" not in digest


def test_build_classification_pack_digest_by_name(policy):
    digest = build_classification_pack_digest("telecom")
    assert digest["pack"] == "telecom"
    assert digest["entity_to_tag"]["PHONE_NUMBER"] == "TelcoDataType:MSISDN"


def test_phone_multi_classification_reasoning(policy):
    prop = {
        "name": "merchant_mobile",
        "logicalType": "string",
        "classification": "pii_personal",
        "tags": ["pii", "gdpr_personal_data"],
        "privacy": {
            "classification": "pii_personal",
            "classification_engine": {
                "entity_type": "PHONE_NUMBER",
                "confidence": 0.97,
                "decision_rule": "regex >= 0.80",
            },
        },
    }
    telemetry = {
        "entity_type": "PHONE_NUMBER",
        "confidence": 0.97,
        "decision_rule": "regex >= 0.80",
        "regex_hits": [{
            "pattern_name": "msisdn_egypt_any",
            "entity_type": "PHONE_NUMBER",
            "score": 0.97,
            "match_rate": 0.96,
        }],
    }
    reasoning = build_deterministic_reasoning(
        prop,
        telemetry,
        policy,
        "telecom.merchants",
    )
    assert reasoning["entity_type"] == "PHONE_NUMBER"
    why = " ".join(reasoning["why"])
    assert "msisdn_egypt_any" in why
    assert "entity_type_mapping" in why
    assert "cotag_matrix" in why
    assert "security_derivation" in why
    assert reasoning["result"]["tags"] == ["MSISDN", "PII"]
    assert reasoning["result"]["classification"] == "pii_personal"
    assert reasoning["result"]["security"] == "Confidential"


def test_enrichment_context_ships_pack_digest_and_reasoning(tmp_path):
    store = ContractStore(LocalBackend(tmp_path / "s"), bucket="active-contracts")
    contract = {
        "apiVersion": "v3.0.1",
        "kind": "DataContract",
        "name": "merchants_contract",
        "version": "1.0.0",
        "status": "active",
        "schema": [{
            "name": "merchants",
            "physicalName": "telecom.merchants",
            "properties": [{
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
            }],
        }],
    }
    store.upsert(contract, table="telecom.merchants", workflow="manual")
    store.metadata.merge_column_telemetry("telecom.merchants", {
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

    from redibis.enrich.context import assemble_enrichment_context
    from redibis.enrich.providers import EnrichmentProvider

    class _P(EnrichmentProvider):
        def complete(self, system_prompt, user_prompt, *, json_mode=True):
            return "{}"

    svc = EnrichmentService(store)
    ctx = svc.build_context("telecom.merchants", _P(model="x"))
    mobile = next(c for c in ctx.column_context if c["name"] == "merchant_mobile")
    assert "deterministic_reasoning" in mobile
    assert mobile["deterministic_reasoning"]["result"]["tags"] == ["MSISDN", "PII"]
    assert "tag_vocabulary" in ctx.pack_digest
    assert "multi_classification" in ctx.pack_digest
    assert "Classification (guided by the pack digest" in ctx.system_prompt

    bundle = assemble_enrichment_context(svc, "telecom.merchants", provider_name="demo")
    assert bundle["docs"]["pack_digest"]["pack"] == "telecom"
    assert "deterministic_reasoning" in bundle["columns"][0]
