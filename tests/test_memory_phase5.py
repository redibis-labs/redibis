"""Phase 5 (T12) — CLI memory commands, scan flags, API hints."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

import pytest

from redibis.cli.memory_cmd import run_memory
from redibis.cli.overrides import apply_cli_overrides
from redibis.config import MemoryConfig, RedibisConfig
from redibis.memory.decision import ReviewDecision
from redibis.memory.embedding import HashEmbeddingProvider
from redibis.memory.store import InMemoryMemoryStore
from redibis.store.contract_store import ContractStore
from redibis.store.storage_backend import LocalBackend


def _args(**kwargs):
    return argparse.Namespace(**kwargs)


def _seed_national_id(store: InMemoryMemoryStore, embed: HashEmbeddingProvider):
    card = "column: national_id | logical_type: string | format: national_id_egypt_strict"
    vec = embed.embed(card)
    store.upsert(
        canonical_key="national_id|string|national_id_egypt_strict",
        fingerprint={
            "name_normalized": "national_id",
            "logical_type": "string",
            "format_signature": "national_id_egypt_strict",
        },
        embedding=vec,
        decision=ReviewDecision(
            fingerprint_key="national_id|string|national_id_egypt_strict",
            table="telecom.customers",
            column="national_id",
            pii_verdict="pii",
            rationale="14-digit national identifier",
            reviewer="cli",
            decided_at=datetime.now(timezone.utc).isoformat(),
        ).to_dict(),
        logical_type="string",
        format_signature="national_id_egypt_strict",
        column_card=card,
    )


def test_apply_cli_overrides_memory_and_tier_flags():
    cfg = RedibisConfig.default()
    args = _args(
        enable_metadata=True,
        enable_pushdown=True,
        enable_memory=True,
        memory_domain="telecom",
        memory_store="memory",
        source_engine="hive",
    )
    apply_cli_overrides(cfg, args)
    assert cfg.profiling.metadata.enabled is True
    assert cfg.profiling.pushdown.enabled is True
    assert cfg.memory.enabled is True
    assert cfg.memory.domain == "telecom"
    assert cfg.memory.store == "memory"
    assert cfg.source.engine == "hive"


def test_memory_status_disabled(capsys):
    code = run_memory(_args(memory_action="status"), ContractStore(
        LocalBackend("/tmp/unused"), bucket="b",
    ))
    assert code == 0
    out = capsys.readouterr().out
    assert "enabled: False" in out


def test_memory_init_db_in_memory_store(tmp_path):
    cfg_path = tmp_path / "redibis.yaml"
    cfg = RedibisConfig.default()
    cfg.memory.enabled = True
    cfg.memory.store = "memory"
    cfg.memory.embedding_provider = "hash"
    cfg.to_yaml(cfg_path)
    code = run_memory(_args(memory_action="init-db", config=str(cfg_path)), ContractStore(
        LocalBackend(tmp_path / "s"), bucket="b",
    ))
    assert code == 0


def test_memory_search_json(tmp_path):
    cfg_path = tmp_path / "redibis.yaml"
    rb = RedibisConfig.default()
    rb.memory.enabled = True
    rb.memory.store = "memory"
    rb.memory.domain = "telecom"
    rb.memory.embedding_provider = "hash"
    rb.to_yaml(cfg_path)

    backend = LocalBackend(tmp_path / "s")
    mem_cfg = MemoryConfig(
        enabled=True, store="memory", domain="telecom", embedding_provider="hash",
    )
    store = ContractStore(backend, bucket="active-contracts", memory_config=mem_cfg)
    embed = HashEmbeddingProvider()
    _seed_national_id(store._memory_store, embed)
    contract = {
        "apiVersion": "v3.0.1",
        "kind": "DataContract",
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
    code = run_memory(_args(
        memory_action="search",
        table="telecom.customers",
        column=None,
        json=True,
        config=str(cfg_path),
    ), store)
    assert code == 0


def test_memory_api_hints_disabled(tmp_path):
    from fastapi.testclient import TestClient
    from redibis.webapp import backend as web

    client = TestClient(web.app)
    r = client.get("/api/memory/status")
    assert r.status_code == 200
    assert r.json()["enabled"] is False

    r2 = client.get("/api/contracts/any.t/memory/hints")
    assert r2.status_code == 200
    assert r2.json()["enabled"] is False


def test_memory_api_hints_with_store(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from redibis.webapp import backend as web

    backend = LocalBackend(tmp_path / "s")
    mem_cfg = MemoryConfig(
        enabled=True, store="memory", domain="telecom", embedding_provider="hash",
    )
    store = ContractStore(backend, bucket="active-contracts", memory_config=mem_cfg)
    embed = HashEmbeddingProvider()
    _seed_national_id(store._memory_store, embed)
    contract = {
        "apiVersion": "v3.0.1",
        "kind": "DataContract",
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
    monkeypatch.setattr(web, "get_contract_store", lambda: store)

    client = TestClient(web.app)
    status = client.get("/api/memory/status").json()
    assert status["enabled"] is True

    hints = client.get("/api/contracts/telecom.customers/memory/hints").json()
    assert hints["enabled"] is True
    assert len(hints["columns"]) >= 1
    assert hints["columns"][0]["column"] == "national_id"


def test_memory_cli_list(tmp_path, capsys):
    cfg_path = tmp_path / "cfg.yaml"
    rb = RedibisConfig.default()
    rb.memory.enabled = True
    rb.memory.store = "memory"
    rb.memory.domain = "telecom"
    rb.memory.embedding_provider = "hash"
    rb.to_yaml(cfg_path)

    backend = LocalBackend(tmp_path / "s")
    mem_cfg = MemoryConfig(
        enabled=True, store="memory", domain="telecom", embedding_provider="hash",
    )
    store = ContractStore(backend, bucket="active-contracts", memory_config=mem_cfg)
    embed = HashEmbeddingProvider()
    _seed_national_id(store._memory_store, embed)

    code = run_memory(_args(
        memory_action="list",
        table=None,
        workflow=None,
        limit=50,
        json=True,
        config=str(cfg_path),
    ), store)
    assert code == 0
    payload = __import__("json").loads(capsys.readouterr().out)
    assert payload["count"] == 1
    assert payload["entries"][0]["column"] == "national_id"
    assert payload["entries"][0]["pii_verdict"] == "pii"

    code2 = run_memory(_args(
        memory_action="list",
        table="telecom.other",
        workflow=None,
        limit=50,
        json=False,
        config=str(cfg_path),
    ), store)
    assert code2 == 0
    assert "No column memory entries" in capsys.readouterr().out
