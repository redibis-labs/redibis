"""Compliance tests for column memory — no raw PII persistence, redaction, embeddings.

Regression guard: ``test_persisted_memory_row_has_no_raw_values_or_digests`` — any change
that leaks raw sample values or unsalted digests into pgvector rows must fail CI.
"""

from __future__ import annotations

import hashlib
import json
import pandas as pd
import pytest

from redibis.config import ConfigError, MemoryConfig, RedibisConfig
from redibis.memory.decision import ReviewDecision
from redibis.memory.embedding import HashEmbeddingProvider
from tests.embedding_helpers import require_sentence_transformer
from redibis.memory.fingerprint import (
    ColumnFingerprint,
    FingerprintField,
    build_fingerprints,
)
from redibis.memory.redaction import memory_safe_shape, redact_samples
from redibis.memory.store import InMemoryMemoryStore, _cosine
from redibis.memory.writer import record_review
from redibis.metadata.base import CatalogColumnStats, ColumnCatalogInfo, SourceMetadata
from redibis.models import ColumnProfile


def _national_id_fixture_df() -> pd.DataFrame:
    return pd.DataFrame({"national_id": ["29001011401234", "29001011401235"]})


_RAW_NATIONAL_IDS = ("29001011401234", "29001011401235")


def _assert_persisted_memory_row_is_safe(row: dict) -> None:
    """No raw sample values or unsalted SHA-256 digests anywhere in a stored row."""
    blob = json.dumps(row, default=str)
    for raw in _RAW_NATIONAL_IDS:
        assert raw not in blob, f"raw value {raw!r} found in persisted memory row"
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        assert digest not in blob, f"unsalted digest for {raw!r} found in persisted memory row"


def test_persisted_memory_row_has_no_raw_values_or_digests():
    """Canonical regression guard for compliance #1 and #2 (column_card + full row)."""
    fps = build_fingerprints(
        _national_id_fixture_df(),
        SourceMetadata(engine="hive", columns=[]),
        [
            ColumnProfile(
                column="national_id",
                dtype="object",
                cardinality_ratio=1.0,
                avg_value_length=14.0,
                null_rate=0.0,
                name_hint_score=0.8,
                arabic_fraction=0.0,
                triage_score=0.9,
                send_to_detector=True,
                logical_type="string",
                physical_type="string",
            ),
        ],
        table="telecom.customers",
        domain="telecom",
    )
    fp = fps[0]
    store = InMemoryMemoryStore()
    cfg = MemoryConfig(
        enabled=True, store="memory", embedding_provider="hash", async_writes=False,
    )
    record_review(
        memory_config=cfg,
        fingerprint=fp,
        decision=ReviewDecision("", "telecom.customers", "national_id", pii_verdict="pii"),
        memory_store=store,
        embedding=HashEmbeddingProvider(),
        sample_values=list(_RAW_NATIONAL_IDS),
    )
    row = store._rows[next(iter(store._rows))]
    _assert_persisted_memory_row_is_safe(row)
    assert "column_card" in row
    _assert_persisted_memory_row_is_safe({"column_card": row["column_card"]})


def test_column_card_omits_sample_only_min_max():
    fp = ColumnFingerprint(
        table="telecom.customers",
        column="national_id",
        domain="telecom",
        name_normalized="national_id",
        logical_type=FingerprintField("string", "hms", "stable"),
        physical_type=FingerprintField("string", "hms", "stable"),
        format_signature=FingerprintField("national_id_egypt_strict", "sample", "stable"),
        cardinality_class=FingerprintField("unique", "sample", "approximate"),
        nullable=FingerprintField(False, "hms", "stable"),
        stats={
            "min": FingerprintField("29001011401234", "sample", "sample_only"),
            "max": FingerprintField("29001011401235", "sample", "sample_only"),
            "null_rate": FingerprintField(0.0, "sample", "sample_only"),
        },
    )
    card = fp.to_column_card()
    assert "29001011401234" not in card
    assert "29001011401235" not in card
    assert "min(" not in card
    assert "max(" not in card


def test_record_review_persists_safe_column_card_only():
    fps = build_fingerprints(
        _national_id_fixture_df(),
        SourceMetadata(engine="hive", columns=[]),
        [
            ColumnProfile(
                column="national_id",
                dtype="object",
                cardinality_ratio=1.0,
                avg_value_length=14.0,
                null_rate=0.0,
                name_hint_score=0.8,
                arabic_fraction=0.0,
                triage_score=0.9,
                send_to_detector=True,
                logical_type="string",
                physical_type="string",
            ),
        ],
        table="telecom.customers",
        domain="telecom",
    )
    fp = fps[0]
    store = InMemoryMemoryStore()
    cfg = MemoryConfig(enabled=True, store="memory", embedding_provider="hash")
    record_review(
        memory_config=cfg,
        fingerprint=fp,
        decision=ReviewDecision("", "telecom.customers", "national_id", pii_verdict="pii"),
        memory_store=store,
        embedding=HashEmbeddingProvider(),
    )
    row = store._rows[next(iter(store._rows))]
    _assert_persisted_memory_row_is_safe(row)


def test_consent_redaction_uses_length_prefixed_shapes_not_hashes(consent_store):
    consent_store.set_approved("t.t", "national_id", approved=True, approved_by="steward")
    raw = "29001011401234"
    out = redact_samples(
        [raw],
        table="t.t",
        column="national_id",
        consent=consent_store,
    )
    assert raw not in out[0]
    assert out[0] == memory_safe_shape(raw)
    assert out[0].startswith("n14:")


def test_hash_embedder_lacks_semantic_similarity_between_synonyms():
    embed = HashEmbeddingProvider()
    card_nid = (
        "column: national_id | logical_type: string | format: national_id_egypt_strict"
    )
    card_short = (
        "column: nid | logical_type: string | format: national_id_egypt_strict"
    )
    sim = _cosine(embed.embed(card_nid), embed.embed(card_short))
    assert sim < 0.35


def test_sentence_transformer_embedder_semantic_similarity_for_synonyms():
    embed = require_sentence_transformer()
    card_nid = (
        "column: national_id | logical_type: string | format: national_id_egypt_strict"
    )
    card_short = (
        "column: nid | logical_type: string | format: national_id_egypt_strict"
    )
    sim = _cosine(embed.embed(card_nid), embed.embed(card_short))
    assert sim > 0.45


def test_validate_rejects_hash_embedder_when_memory_enabled():
    with pytest.raises(ConfigError, match="hash embedder"):
        RedibisConfig.from_dict({
            "memory": {"enabled": True, "embedding_provider": "hash", "store": "pgvector"},
        })


def test_fingerprint_null_rate_from_catalog_not_sample():
    df = pd.DataFrame({"national_id": ["29001011401234", "29001011401235"]})
    source_meta = SourceMetadata(
        engine="hive",
        columns=[
            ColumnCatalogInfo(
                name="national_id", ordinal=0, native_type="string", nullable=False,
            ),
        ],
        column_stats={
            "national_id": CatalogColumnStats(ndv=2, num_nulls=0, stats_fresh=True),
        },
    )
    fps = build_fingerprints(
        df,
        source_meta,
        [
            ColumnProfile(
                column="national_id",
                dtype="object",
                cardinality_ratio=1.0,
                avg_value_length=14.0,
                null_rate=0.5,
                name_hint_score=0.8,
                arabic_fraction=0.0,
                triage_score=0.9,
                send_to_detector=True,
                logical_type="string",
                physical_type="string",
            ),
        ],
        table="telecom.customers",
    )
    fp = fps[0]
    assert fp.stats["null_rate"].source == "hms"
    assert fp.stats["null_rate"].generalizes == "approximate"
    assert fp.cardinality_class.source == "hms"


@pytest.fixture
def consent_store(tmp_path):
    from redibis.store.contract_store import ContractStore
    from redibis.store.storage_backend import LocalBackend
    from redibis.memory.consent import SamplingConsentStore

    backend = LocalBackend(str(tmp_path / "storage"))
    store = ContractStore(backend, bucket="active-contracts")
    return SamplingConsentStore(store.backend, store.bucket)
