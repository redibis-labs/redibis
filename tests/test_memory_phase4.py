"""Phase 4 — context retrieval and enrichment prompt injection."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from redibis.config import MemoryConfig
from redibis.enrich.providers import EnrichmentProvider
from redibis.enrich.service import EnrichmentService
from redibis.memory.decision import ReviewDecision
from redibis.memory.embedding import HashEmbeddingProvider
from redibis.memory.fingerprint import ColumnFingerprint, FingerprintField
from redibis.memory.retriever import ContextRetriever, get_context_retriever
from redibis.memory.store import InMemoryMemoryStore
from redibis.store.contract_store import ContractStore
from redibis.store.storage_backend import LocalBackend


@pytest.fixture
def memory_store():
    return InMemoryMemoryStore()


@pytest.fixture
def embedder():
    return HashEmbeddingProvider()


@pytest.fixture
def memory_config():
    return MemoryConfig(
        enabled=True, store="memory", domain="telecom", top_k=3, embedding_provider="hash",
    )


def _national_id_fingerprint(domain: str = "telecom") -> ColumnFingerprint:
    return ColumnFingerprint(
        table="finance.accounts",
        column="national_id",
        domain=domain,
        name_normalized="national_id",
        logical_type=FingerprintField("string", "sample", "stable"),
        physical_type=FingerprintField("string", "sample", "stable"),
        format_signature=FingerprintField("national_id_egypt_strict", "sample", "stable"),
        cardinality_class=FingerprintField("unique", "sample", "approximate"),
        nullable=FingerprintField(False, "sample", "stable"),
        redacted_samples=["DDDDDDDDDDDDDD"],
    )


def _seed_national_id_memory(store: InMemoryMemoryStore, embed: HashEmbeddingProvider):
    fp = {
        "name_normalized": "national_id",
        "logical_type": "string",
        "format_signature": "national_id_egypt_strict",
        "domain": "telecom",
    }
    card = "column: national_id | logical_type: string | format: national_id_egypt_strict"
    vec = embed.embed(card)
    store.upsert(
        canonical_key="national_id|string|national_id_egypt_strict",
        fingerprint=fp,
        embedding=vec,
        decision=ReviewDecision(
            fingerprint_key="national_id|string|national_id_egypt_strict",
            table="telecom.customers",
            column="national_id",
            pii_verdict="pii",
            classification="pii_sensitive",
            rationale="14-digit national identifier",
            reviewer="session:test",
            decided_at=datetime.now(timezone.utc).isoformat(),
            contract_version="v3",
        ).to_dict(),
        domain="telecom",
        logical_type="string",
        format_signature="national_id_egypt_strict",
        column_card=card,
    )


def _seed_email_memory(store: InMemoryMemoryStore, embed: HashEmbeddingProvider):
    card = "column: email | logical_type: string | format: email_address"
    vec = embed.embed(card)
    store.upsert(
        canonical_key="email|string|email_address",
        fingerprint={"name_normalized": "email", "logical_type": "string",
                       "format_signature": "email_address"},
        embedding=vec,
        decision=ReviewDecision(
            fingerprint_key="email|string|email_address",
            table="other.t",
            column="email",
            pii_verdict="pii",
            rationale="email column",
            reviewer="cli",
        ).to_dict(),
        logical_type="string",
        format_signature="email_address",
        column_card=card,
    )


def test_context_retriever_returns_national_id_reviews(
    memory_store, embedder, memory_config,
):
    _seed_national_id_memory(memory_store, embedder)
    _seed_email_memory(memory_store, embedder)
    retriever = ContextRetriever(memory_config, memory_store, embedding=embedder)
    hits = retriever.retrieve(_national_id_fingerprint())
    assert len(hits) >= 1
    assert hits[0].fingerprint_summary.get("format_signature") == "national_id_egypt_strict"
    assert hits[0].decisions[-1].pii_verdict == "pii"
    assert "national identifier" in hits[0].rationale


def test_context_retriever_excludes_type_format_mismatch(
    memory_store, embedder, memory_config,
):
    _seed_email_memory(memory_store, embedder)
    retriever = ContextRetriever(memory_config, memory_store, embedding=embedder)
    hits = retriever.retrieve(_national_id_fingerprint())
    assert hits == []


def test_rank_score_boosts_matching_domain():
    from redibis.memory.retriever import RetrievedContext, _rank_score

    ctx = RetrievedContext(
        fingerprint_summary={"domain": "telecom"},
        similarity=0.5,
        provenance={"reviewer": "cli", "decided_at": "2026-01-01T00:00:00+00:00"},
    )
    matched = _rank_score(0.5, ctx, query_domain="telecom")
    other = _rank_score(0.5, ctx, query_domain="finance")
    assert matched > other


def test_get_context_retriever_disabled_returns_none():
    assert get_context_retriever(MemoryConfig(enabled=False)) is None


def test_enrich_prompt_unchanged_when_memory_off(tmp_path):
    store = ContractStore(LocalBackend(tmp_path / "s"), bucket="active-contracts")
    contract = {
        "apiVersion": "v3.0.1", "kind": "DataContract",
        "schema": [{"name": "t", "properties": [{"name": "email", "logicalType": "string"}]}],
    }
    store.upsert(contract, table="db.t", workflow="manual", run_id="r1")
    mem_store = InMemoryMemoryStore()
    _seed_email_memory(mem_store, HashEmbeddingProvider())
    retriever = ContextRetriever(
        MemoryConfig(enabled=True, store="memory", embedding_provider="hash"),
        mem_store,
        embedding=HashEmbeddingProvider(),
    )
    svc = EnrichmentService(store, context_retriever=retriever)
    captured = {}

    class Cap(EnrichmentProvider):
        def __init__(self):
            super().__init__(model="m")
            self.name = "cap"
        def complete(self, system_prompt, user_prompt, *, json_mode=True):
            captured["user"] = user_prompt
            return '{"columns": {}}'

    svc.enrich("db.t", Cap(), memory_config=MemoryConfig(enabled=False))
    assert "SIMILAR PAST STEWARD REVIEWS" not in captured["user"]


def test_enrich_prompt_includes_memory_context_when_enabled(tmp_path):
    store = ContractStore(
        LocalBackend(tmp_path / "s"),
        bucket="active-contracts",
        memory_config=MemoryConfig(
            enabled=True, store="memory", domain="telecom", embedding_provider="hash",
        ),
    )
    contract = {
        "apiVersion": "v3.0.1", "kind": "DataContract",
        "schema": [{
            "name": "t",
            "properties": [{
                "name": "national_id",
                "logicalType": "string",
                "privacy": {"classification_engine": {"entity_type": "EG_NATIONAL_ID"}},
            }],
        }],
    }
    store.upsert(contract, table="telecom.customers", workflow="manual", run_id="r1")
    mem_store = InMemoryMemoryStore()
    _seed_national_id_memory(mem_store, HashEmbeddingProvider())
    retriever = ContextRetriever(
        store.memory_config, mem_store, embedding=HashEmbeddingProvider(),
    )
    svc = EnrichmentService(store, context_retriever=retriever)
    captured = {}

    class Cap(EnrichmentProvider):
        def __init__(self):
            super().__init__(model="m")
            self.name = "cap"
        def complete(self, system_prompt, user_prompt, *, json_mode=True):
            captured["user"] = user_prompt
            return '{"columns": {"national_id": {"business": {"definition": "ID"}}}}'

    result = svc.enrich("telecom.customers", Cap())
    assert "SIMILAR PAST STEWARD REVIEWS" in captured["user"]
    assert "suggest-only" in captured["user"]
    assert "national_id" in captured["user"]
    assert result.candidate["enrichment_meta"]["memory_context"] is True
