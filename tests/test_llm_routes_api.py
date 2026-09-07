"""API tests for capability routing routes envelope."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("REDIBIS_CONFIGS_DIR", str(tmp_path / "configs"))
    (tmp_path / "configs").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("REDIBIS_LLM_PROVIDERS", str(tmp_path / "llm_providers.json"))
    Path(tmp_path / "llm_providers.json").write_text("{}", encoding="utf-8")

    from redibis.webapp.store_accessors import clear_stores
    from redibis.webapp import backend

    clear_stores()
    return TestClient(backend.app)


def test_routes_get_put_if_match(client):
    r = client.get("/api/llm/routes")
    assert r.status_code == 200
    body = r.json()
    assert body["revision"] == 0
    assert "settings" in body and "llm" in body["settings"]

    put = client.put(
        "/api/llm/routes",
        headers={"If-Match": "revision:0"},
        json={
            "schema_version": 1,
            "settings": {
                "llm": {
                    "default": {"provider": "sglang", "model": "default"},
                    "roles": {
                        "agent.planner": {"provider": "sglang", "model": "default"},
                        "contract.enrichment": {"provider": "sglang", "model": "default"},
                    },
                }
            },
        },
    )
    assert put.status_code == 200, put.text
    saved = put.json()["routes"]
    assert saved["revision"] == 1
    assert saved["settings"]["llm"]["default"]["provider"] == "sglang"

    conflict = client.put(
        "/api/llm/routes",
        headers={"If-Match": "revision:0"},
        json={
            "schema_version": 1,
            "settings": {"llm": {"default": {"provider": "ollama", "model": "x"}, "roles": {}}},
        },
    )
    assert conflict.status_code == 409

    ok = client.put(
        "/api/llm/routes",
        headers={"If-Match": "revision:1"},
        json={
            "schema_version": 1,
            "settings": {
                "llm": {
                    "default": {"provider": "ollama", "model": "llama"},
                    "roles": {},
                }
            },
        },
    )
    assert ok.status_code == 200
    assert ok.json()["routes"]["revision"] == 2


def test_routes_validate_and_runtime(client):
    bad = client.post(
        "/api/llm/routes/validate",
        json={"settings": {"llm": {"roles": {"not.a.role": {"provider": "x"}}}}},
    )
    assert bad.status_code == 400

    good = client.post(
        "/api/llm/routes/validate",
        json={
            "settings": {
                "llm": {
                    "default": {"provider": "sglang", "model": "default"},
                    "roles": {"agent.planner": {"provider": "sglang", "model": "default"}},
                }
            }
        },
    )
    assert good.status_code == 200
    assert good.json()["ok"] is True

    runtime = client.get("/api/llm/routes/runtime")
    assert runtime.status_code == 200
    roles = runtime.json()["roles"]
    assert "agent.planner" in roles
    assert "contract.enrichment" in roles


def test_calls_filter_params_accepted(client):
    r = client.get("/api/llm/calls?limit=5&model_role=agent.planner")
    assert r.status_code == 200
    assert "calls" in r.json()
