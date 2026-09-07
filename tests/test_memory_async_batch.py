"""Async memory writes (#4) and batched embeddings (#6)."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from redibis.config import MemoryConfig
from redibis.memory.async_writer import flush_memory_writes
from redibis.memory.decision import ReviewDecision
from redibis.memory.embedding import HashEmbeddingProvider
from redibis.memory.fingerprint import ColumnFingerprint, FingerprintField
from redibis.memory.retriever import ContextRetriever, build_memory_context_section
from redibis.memory.store import InMemoryMemoryStore
from redibis.memory import writer as memory_writer
from redibis.memory.writer import record_approved_merge


@pytest.fixture
def memory_config():
    return MemoryConfig(
        enabled=True, store="memory", domain="telecom", top_k=3, embedding_provider="hash",
    )


class CountingEmbedder(HashEmbeddingProvider):
    def __init__(self) -> None:
        super().__init__()
        self.embed_calls = 0
        self.embed_batch_calls = 0

    def embed(self, text: str) -> list[float]:
        self.embed_calls += 1
        return super().embed(text)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.embed_batch_calls += 1
        return [HashEmbeddingProvider.embed(self, text) for text in texts]


def _column_fp(name: str) -> ColumnFingerprint:
    return ColumnFingerprint(
        table="db.t",
        column=name,
        domain="telecom",
        name_normalized=name,
        logical_type=FingerprintField("string", "contract", "stable"),
        physical_type=FingerprintField("string", "contract", "stable"),
        format_signature=FingerprintField("email_address", "contract", "stable"),
        cardinality_class=FingerprintField("unknown", "contract", "approximate"),
        nullable=FingerprintField(True, "contract", "stable"),
    )


def test_retrieve_single_uses_embed_not_batch(memory_config):
    embedder = CountingEmbedder()
    store = InMemoryMemoryStore()
    retriever = ContextRetriever(memory_config, store, embedding=embedder)
    retriever.retrieve(_column_fp("email"))
    assert embedder.embed_calls == 1
    assert embedder.embed_batch_calls == 0


def test_retrieve_batch_uses_embed_batch_once(memory_config):
    embedder = CountingEmbedder()
    store = InMemoryMemoryStore()
    retriever = ContextRetriever(memory_config, store, embedding=embedder)
    fps = [_column_fp(f"col_{i}") for i in range(5)]
    retriever.retrieve_batch(fps)
    assert embedder.embed_batch_calls == 1
    assert embedder.embed_calls == 0


def test_build_memory_context_batches_embeddings(memory_config):
    embedder = CountingEmbedder()
    store = InMemoryMemoryStore()
    retriever = ContextRetriever(memory_config, store, embedding=embedder)
    contract = {
        "schema": [{
            "properties": [
                {"name": f"col_{i}", "logicalType": "string"}
                for i in range(8)
            ],
        }],
    }
    build_memory_context_section("db.t", contract, retriever)
    assert embedder.embed_batch_calls == 1
    assert embedder.embed_calls == 0


def test_record_approved_merge_offloads_writes_by_default(monkeypatch):
    finished = threading.Event()
    original = memory_writer.record_review

    def slow_record_review(**kwargs):
        time.sleep(0.15)
        finished.set()
        return original(**kwargs)

    monkeypatch.setattr(memory_writer, "record_review", slow_record_review)

    item = MagicMock()
    item.status = "merged"
    item.column = "email"
    item.kind = "pii"
    item.payload = {"classification": "pii_personal"}
    item.source = "scan"
    item.source_run_id = "r1"

    session = MagicMock()
    session.approved.items = [item]
    session.table_name = "db.t"
    session.session_id = "sess-1"

    store = MagicMock()
    store.backend = MagicMock()
    store.bucket = "contracts"
    store._memory_store = InMemoryMemoryStore()

    cfg = MemoryConfig(
        enabled=True, store="memory", embedding_provider="hash", async_writes=True,
    )
    record_approved_merge(session, store, memory_config=cfg, merged_version="v2")
    assert not finished.is_set()
    flush_memory_writes()
    assert finished.is_set()


def test_memory_write_stats_track_queue_and_failures(monkeypatch):
    from redibis.memory.async_writer import memory_write_stats, submit_memory_task

    before = memory_write_stats()
    submit_memory_task(lambda: None)
    flush_memory_writes()
    after = memory_write_stats()
    assert after.tasks_submitted >= before.tasks_submitted + 1
    assert after.tasks_completed >= before.tasks_completed + 1
    assert after.queue_pending == 0

    def boom() -> None:
        raise RuntimeError("embedder down")

    submit_memory_task(boom)
    flush_memory_writes()
    failed = memory_write_stats()
    assert failed.tasks_failed >= before.tasks_failed + 1


def test_record_approved_merge_sync_when_async_disabled(monkeypatch):
    finished = threading.Event()
    original = memory_writer.record_review

    def slow_record_review(**kwargs):
        time.sleep(0.05)
        finished.set()
        return original(**kwargs)

    monkeypatch.setattr(memory_writer, "record_review", slow_record_review)

    item = MagicMock()
    item.status = "merged"
    item.column = "email"
    item.kind = "pii"
    item.payload = {"classification": "pii_personal"}
    item.source = "scan"
    item.source_run_id = "r1"

    session = MagicMock()
    session.approved.items = [item]
    session.table_name = "db.t"
    session.session_id = "sess-1"

    store = MagicMock()
    store.backend = MagicMock()
    store.bucket = "contracts"
    store._memory_store = InMemoryMemoryStore()

    cfg = MemoryConfig(
        enabled=True, store="memory", embedding_provider="hash", async_writes=False,
    )
    record_approved_merge(session, store, memory_config=cfg, merged_version="v2")
    assert finished.is_set()
