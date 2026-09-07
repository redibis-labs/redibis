"""Phase 3 — consent, redaction, embedding store, review-decision memory writes."""

from __future__ import annotations

import json

import pytest

from redibis.config import MemoryConfig
from redibis.memory.consent import SamplingConsentStore
from redibis.memory.decision import ReviewDecision
from tests.embedding_helpers import require_sentence_transformer
from redibis.memory.embedding import HashEmbeddingProvider, get_embedding_provider
from redibis.memory.fingerprint import ColumnFingerprint, FingerprintField
from redibis.memory.rationale import derive_rationale
from redibis.memory.redaction import redact_samples
from redibis.memory.store import InMemoryMemoryStore, canonical_fingerprint_key, get_memory_store, _cosine
from redibis.memory.writer import fingerprint_from_column_prop, record_review
from redibis.store.contract_store import ContractStore
from redibis.store.storage_backend import LocalBackend


@pytest.fixture
def contract_store(tmp_path):
    backend = LocalBackend(str(tmp_path / "storage"))
    return ContractStore(backend, bucket="active-contracts")


@pytest.fixture
def consent_store(contract_store):
    return SamplingConsentStore(contract_store.backend, contract_store.bucket)


@pytest.fixture
def sample_fingerprint():
    return ColumnFingerprint(
        table="telecom.customers",
        column="national_id",
        domain="telecom",
        name_normalized="national_id",
        logical_type=FingerprintField("string", "hms", "stable"),
        physical_type=FingerprintField("string", "hms", "stable"),
        format_signature=FingerprintField("national_id_egypt_strict", "sample", "stable"),
        cardinality_class=FingerprintField("unique", "sample", "approximate"),
        nullable=FingerprintField(False, "hms", "stable"),
        redacted_samples=["DDDDDDDDDDDDDD"],
    )


def test_redact_without_consent_uses_shape_masks_only():
    out = redact_samples(
        ["29001011401234", "29001011401235"],
        table="t.t",
        column="national_id",
        approved=False,
    )
    assert "29001011401234" not in json.dumps(out)
    assert all("D" in s for s in out)


def test_redact_with_consent_uses_shape_masks_not_raw(consent_store):
    consent_store.set_approved("t.t", "national_id", approved=True, approved_by="steward")
    out = redact_samples(
        ["29001011401234"],
        table="t.t",
        column="national_id",
        consent=consent_store,
    )
    assert "29001011401234" not in out[0]
    assert out[0].startswith("n14:")
    assert "D" in out[0]


def test_sampling_consent_store_round_trip(consent_store):
    consent_store.set_approved("db.t", "email", approved=True, approved_by="alice")
    assert consent_store.is_approved("db.t", "email") is True
    assert consent_store.is_approved("db.t", "phone") is False


def test_derive_rationale_from_decision(sample_fingerprint):
    decision = ReviewDecision(
        fingerprint_key="",
        table="telecom.customers",
        column="national_id",
        pii_verdict="pii",
        classification="pii_sensitive",
        provenance={"workflow": "approved_merge"},
    )
    text = derive_rationale(decision, sample_fingerprint)
    assert "national_id" in text
    assert "29001011401234" not in text
    assert "pii_sensitive" in text


def test_memory_store_canonical_upsert_increments_occurrences():
    store = InMemoryMemoryStore()
    embed = HashEmbeddingProvider()
    fp = {
        "name_normalized": "national_id",
        "logical_type": "string",
        "format_signature": "national_id_egypt_strict",
    }
    key = canonical_fingerprint_key("national_id", "string", "national_id_egypt_strict")
    decision = {"column": "national_id", "pii_verdict": "pii"}
    vec = embed.embed("national_id string")
    r1 = store.upsert(
        canonical_key=key, fingerprint=fp, embedding=vec, decision=decision,
        logical_type="string", format_signature="national_id_egypt_strict",
        column_card="national_id",
    )
    r2 = store.upsert(
        canonical_key=key, fingerprint=fp, embedding=vec, decision=decision,
        logical_type="string", format_signature="national_id_egypt_strict",
        column_card="national_id",
    )
    assert r1.created is True
    assert r2.created is False
    assert r2.occurrence_count == 2


def test_memory_store_search_filters_type_and_format():
    store = InMemoryMemoryStore()
    embed = HashEmbeddingProvider()
    base_vec = embed.embed("national_id string national_id_egypt_strict")
    store.upsert(
        canonical_key="national_id|string|national_id_egypt_strict",
        fingerprint={},
        embedding=base_vec,
        decision={"pii_verdict": "pii"},
        logical_type="string",
        format_signature="national_id_egypt_strict",
    )
    store.upsert(
        canonical_key="score|integer|numeric",
        fingerprint={},
        embedding=embed.embed("score integer numeric"),
        decision={},
        logical_type="integer",
        format_signature="numeric",
    )
    hits = store.search(
        base_vec,
        top_k=5,
        logical_type="string",
        format_signature="national_id_egypt_strict",
    )
    assert len(hits) == 1
    assert hits[0].canonical_key.startswith("national_id")


def test_record_review_noop_when_memory_disabled(sample_fingerprint):
    cfg = MemoryConfig(enabled=False)
    assert record_review(
        memory_config=cfg,
        fingerprint=sample_fingerprint,
        decision=ReviewDecision("", "t", "c"),
    ) is None


def test_record_review_writes_when_enabled(sample_fingerprint):
    cfg = MemoryConfig(enabled=True, store="memory", embedding_provider="hash")
    mem = InMemoryMemoryStore()
    decision = ReviewDecision(
        fingerprint_key="",
        table="telecom.customers",
        column="national_id",
        pii_verdict="pii",
        reviewer="tester",
        contract_version="v2",
    )
    out = record_review(
        memory_config=cfg,
        fingerprint=sample_fingerprint,
        decision=decision,
        memory_store=mem,
        embedding=HashEmbeddingProvider(),
    )
    assert out is not None
    assert out["occurrence_count"] == 1
    assert decision.rationale
    assert "29001011401234" not in decision.rationale


def test_get_memory_store_disabled_returns_none():
    assert get_memory_store(MemoryConfig(enabled=False)) is None


def test_get_embedding_provider_default_is_semantic():
    require_sentence_transformer()  # ensure model reachable before default provider path
    from redibis.memory.embedding import get_embedding_provider

    p = get_embedding_provider(MemoryConfig())
    card_a = "column: national_id | logical_type: string"
    card_b = "column: nid | logical_type: string"
    sim = _cosine(p.embed(card_a), p.embed(card_b))
    assert sim > 0.45


def test_get_embedding_provider_local():
    p = get_embedding_provider(MemoryConfig(embedding_provider="local"))
    vec = p.embed("hello")
    assert len(vec) == p.dimension


def test_set_pii_decision_memory_hook_noop_when_disabled(contract_store, tmp_path):
    backend = LocalBackend(str(tmp_path / "s2"))
    store = ContractStore(backend, bucket="active-contracts", memory_config=MemoryConfig(enabled=False))
    partial = {
        "apiVersion": "v3.0.1",
        "kind": "DataContract",
        "database_name": "db",
        "table_name": "t",
        "schema": [{"name": "db_t", "physicalName": "db.t", "properties": [
            {"name": "email", "logicalType": "string"},
        ]}],
    }
    store.upsert(partial=partial, table="db.t", workflow="bootstrap", run_id="r1")
    before = store.get_active("db.t")
    store.set_pii_decision("db.t", "email", "pii", payload={
        "name": "email",
        "classification": "pii_personal",
        "privacy": {"classification_engine": {"entity_type": "EMAIL"}},
    })
    after = store.get_active("db.t")
    assert before is not None and after is not None


def test_fingerprint_from_column_prop_never_contains_raw_samples():
    fp = fingerprint_from_column_prop(
        "db.t",
        "national_id",
        {"name": "national_id", "logicalType": "string"},
        sample_values=["29001011401234"],
    )
    card = fp.to_column_card()
    assert "29001011401234" not in card
