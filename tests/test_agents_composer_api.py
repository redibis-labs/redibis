"""Tests for agent composer backend endpoints (pack library + source)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("USE_LOCAL_STORAGE", "true")
    monkeypatch.setenv("LOCAL_STORAGE_ROOT", str(tmp_path / "storage"))
    from redibis.webapp.backend import app

    return TestClient(app)


def _enable_agents(monkeypatch, tmp_path):
    import redibis.webapp.backend as web
    from redibis.config import RedibisConfig

    cfg = RedibisConfig.default()
    cfg.agents.enabled = True
    cfg.agents.runs_dir = str(tmp_path / "agent_runs")
    monkeypatch.setattr(web, "_redibis_config", lambda: cfg)
    return web


def test_agents_packs_list(client):
    res = client.get("/api/agents/packs")
    assert res.status_code == 200
    data = res.json()
    assert "telecom" in data.get("packs", [])
    assert data["policy"] == data["classification"]


def test_agents_pack_get_and_save(client, tmp_path, monkeypatch):
    _enable_agents(monkeypatch, tmp_path)
    monkeypatch.setenv("REDIBIS_PACK_DIR", str(tmp_path / "packs"))

    fixture = Path(__file__).parent / "fixtures" / "catalog_telecom_customers.yaml"
    # minimal valid pack shape from builtin telecom
    from redibis.classification.pack_store import get_pack_text
    text = get_pack_text("telecom")

    res = client.put("/api/agents/packs/my_custom", json={"text": text})
    assert res.status_code == 200

    res2 = client.get("/api/agents/packs/my_custom")
    assert res2.status_code == 200
    assert res2.json()["text"] == text


def test_agents_source_test_oracle_mock(client, tmp_path, monkeypatch):
    _enable_agents(monkeypatch, tmp_path)
    mock_retriever = MagicMock()
    mock_retriever.list_tables.return_value = ["CUSTOMERS", "ORDERS"]

    import redibis.webapp.backend as web

    monkeypatch.setattr(web, "_source_retriever", lambda body, spark=None: mock_retriever)

    res = client.post("/api/agents/source/test", json={
        "engine": "oracle",
        "jdbc": {"host": "x", "port": 1521, "database": "ORCL", "credential_ref": "CREDS"},
    })
    assert res.status_code == 200
    data = res.json()
    assert data["connected"] is True
    assert data["table_count"] == 2


def test_agents_source_upload(client, tmp_path, monkeypatch):
    _enable_agents(monkeypatch, tmp_path)
    sess = client.post("/api/agents/source/samples/session")
    assert sess.status_code == 200
    session_id = sess.json()["session_id"]

    res = client.post(
        f"/api/agents/source/upload?session_id={session_id}",
        files=[("files", ("telecom_customers.csv", b"a,b\n1,2\n", "text/csv"))],
    )
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert "telecom_customers" in data["tables"]

    res2 = client.get(f"/api/agents/source/samples?session_id={session_id}")
    assert res2.status_code == 200
    assert "telecom_customers" in res2.json()["tables"]

    empty = client.get("/api/agents/source/samples")
    assert empty.status_code == 200
    assert empty.json()["tables"] == []

    res3 = client.delete(
        f"/api/agents/source/samples/telecom_customers.csv?session_id={session_id}",
    )
    assert res3.status_code == 200
    assert res3.json()["tables"] == []


def test_agents_defaults_get(client):
    res = client.get("/api/agents/defaults")
    assert res.status_code == 200
    data = res.json()
    assert data["defaults"]["pii"]["engines"] == "both"
    assert "engines: both" in data["text"] or "engines: both" in data["text"].replace('"', "")


def test_agents_packs_includes_general(client):
    res = client.get("/api/agents/packs")
    assert res.status_code == 200
    assert "telecom" in res.json()["packs"]
    assert "general" in res.json()["packs"]


def test_agents_source_schemas_jdbc_mock(client, tmp_path, monkeypatch):
    _enable_agents(monkeypatch, tmp_path)
    mock_retriever = MagicMock()
    mock_retriever.list_schemas.return_value = ["public", "sales"]

    import redibis.webapp.backend as web

    monkeypatch.setattr(web, "_source_retriever", lambda body, spark=None: mock_retriever)

    res = client.get("/api/agents/source/schemas?engine=postgres&host=localhost&database=db")
    assert res.status_code == 200
    assert res.json()["schemas"] == ["public", "sales"]
