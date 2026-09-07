"""Tests for LLM provider connectivity test API."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from redibis.webapp.backend import app

    return TestClient(app)


def test_demo_provider_test_ok_offline(client):
    r = client.post("/api/llm-providers/demo/test", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["provider"] == "demo"
    assert body["latency_ms"] >= 0


def test_provider_test_auth_error_redacts_key(client, monkeypatch):
    from redibis.enrich.providers import EnrichmentError, LiteLLMProvider

    class _StubProvider(LiteLLMProvider):
        def complete(self, system_prompt, user_prompt, *, json_mode=True):
            raise EnrichmentError("auth failed for key sk-badtoken99")

    def _fake_get_provider(name, **kwargs):
        if name == "openai":
            return _StubProvider(name="openai", litellm_model="openai/gpt-4o")
        raise ValueError(f"unknown {name}")

    monkeypatch.setattr("redibis.enrich.probe.get_provider", _fake_get_provider)

    r = client.post(
        "/api/llm-providers/openai/test",
        json={"api_key": "sk-badtoken99"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["error"]
    assert "sk-badtoken99" not in body.get("log", "")
    assert "sk-badtoken99" not in body.get("error", "")


def test_ephemeral_api_key_not_persisted_to_registry(client, tmp_path, monkeypatch):
    from redibis.enrich import providers as prov_mod

    registry = tmp_path / "llm_providers.json"
    registry.write_text(
        json.dumps({"providers": {"openai": {"litellm_model": "openai/gpt-4o", "api_key_env": "OPENAI_API_KEY"}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("REDIBIS_LLM_PROVIDERS", str(registry))

    before = registry.read_text(encoding="utf-8")

    with patch.object(
        prov_mod.LiteLLMProvider,
        "complete",
        return_value="OK",
    ):
        r = client.post(
            "/api/llm-providers/openai/test",
            json={"api_key": "sk-ephemeral-should-not-save"},
        )

    assert r.status_code == 200
    after = registry.read_text(encoding="utf-8")
    assert after == before
    assert "sk-ephemeral" not in after


def test_env_status_reports_set_without_value(client, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "secret-value")
    r = client.get("/api/llm-providers/openai/env-status")
    assert r.status_code == 200
    body = r.json()
    assert body["api_key_env"] == "OPENAI_API_KEY"
    assert body["set"] is True
    assert "secret" not in json.dumps(body)


def test_registry_endpoint_hides_saved_key_but_marks_it_present(client, tmp_path, monkeypatch):
    registry = tmp_path / "llm_providers.json"
    registry.write_text(
        json.dumps({
            "providers": {
                "openai": {
                    "litellm_model": "openai/gpt-4o",
                    "api_key": "sk-saved-secret",
                }
            }
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("REDIBIS_LLM_PROVIDERS", str(registry))

    r = client.get("/api/llm-providers/registry")
    assert r.status_code == 200
    body = r.json()
    cfg = body["providers"]["openai"]
    assert cfg["api_key_saved"] is True
    assert "api_key" not in cfg
    assert "sk-saved-secret" not in json.dumps(body)


def test_save_user_provider_configs_strips_existing_keys_and_rejects_literals(tmp_path):
    from redibis.enrich.providers import EnrichmentError, save_user_provider_configs

    registry = tmp_path / "llm_providers.json"
    registry.write_text(
        json.dumps({
            "providers": {
                "openai": {
                    "litellm_model": "openai/gpt-4o",
                    "api_key": "sk-old-secret",
                    "api_key_env": "OPENAI_API_KEY",
                    "api_base": "https://old.example",
                }
            }
        }),
        encoding="utf-8",
    )

    save_user_provider_configs(
        {
            "openai": {
                "litellm_model": "openai/gpt-4o",
                "api_key_env": "OPENAI_API_KEY",
                "api_base": "https://new.example",
            }
        },
        config_path=str(registry),
    )

    saved = json.loads(registry.read_text(encoding="utf-8"))
    assert "api_key" not in saved["providers"]["openai"]
    assert saved["providers"]["openai"]["api_base"] == "https://new.example"
    assert "sk-old-secret" not in registry.read_text(encoding="utf-8")

    with pytest.raises(EnrichmentError, match="literal api_key"):
        save_user_provider_configs(
            {
                "openai": {
                    "litellm_model": "openai/gpt-4o",
                    "api_key": "sk-new-secret",
                }
            },
            config_path=str(registry),
        )


def test_save_user_provider_configs_rejects_api_key_in_api_base(tmp_path):
    from redibis.enrich.providers import EnrichmentError, save_user_provider_configs

    registry = tmp_path / "llm_providers.json"
    with pytest.raises(EnrichmentError, match="looks like an API key"):
        save_user_provider_configs(
            {"gemini": {"api_base": "AIzaSyExampleNotARealKey123456789"}},
            config_path=str(registry),
        )


def test_registry_save_rejects_api_key_in_api_base(client, tmp_path, monkeypatch):
    registry = tmp_path / "llm_providers.json"
    registry.write_text(json.dumps({"providers": {}}), encoding="utf-8")
    monkeypatch.setenv("REDIBIS_LLM_PROVIDERS", str(registry))

    r = client.put(
        "/api/llm-providers/registry",
        json={"providers": {"gemini": {"api_base": "AIzaSyExampleNotARealKey123456789"}}},
    )
    assert r.status_code == 400
    assert "looks like an API key" in r.json()["detail"]


def test_llm_providers_lists_default_provider(client):
    r = client.get("/api/llm-providers")
    assert r.status_code == 200
    body = r.json()
    assert body.get("default_provider") == "gemini"


def test_provider_test_non_enrichment_error_returns_ok_false(client, monkeypatch):
    from redibis.enrich.providers import LiteLLMProvider

    class _StubProvider(LiteLLMProvider):
        def complete(self, system_prompt, user_prompt, *, json_mode=True):
            raise RuntimeError("connection reset")

    def _fake_get_provider(name, **kwargs):
        if name == "openai":
            return _StubProvider(name="openai", litellm_model="openai/gpt-4o")
        raise ValueError(f"unknown {name}")

    monkeypatch.setattr("redibis.enrich.probe.get_provider", _fake_get_provider)

    r = client.post("/api/llm-providers/openai/test", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "connection reset" in body["error"]


def test_provider_test_custom_prompt(client, monkeypatch):
    from redibis.enrich.providers import LiteLLMProvider

    seen = {}

    class _StubProvider(LiteLLMProvider):
        def complete(self, system_prompt, user_prompt, *, json_mode=True):
            seen["user_prompt"] = user_prompt
            return "Hello from the model"

    def _fake_get_provider(name, **kwargs):
        if name == "gemini":
            return _StubProvider(name="gemini", litellm_model="gemini/gemini-2.0-flash")
        raise ValueError(f"unknown {name}")

    monkeypatch.setattr("redibis.enrich.probe.get_provider", _fake_get_provider)

    r = client.post(
        "/api/llm-providers/gemini/test",
        json={"model": "gemini-2.0-flash", "prompt": "Say hello"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["response_snippet"] == "Hello from the model"
    assert seen["user_prompt"] == "Say hello"


def test_lifespan_shutdown_jobs(monkeypatch):
    from unittest.mock import MagicMock
    import redibis.webapp.backend as web

    called = {"n": 0}
    mock_shutdown = MagicMock(side_effect=lambda wait=False: called.__setitem__("n", called["n"] + 1))
    monkeypatch.setattr("redibis.webapp.jobs.shutdown_jobs", mock_shutdown)

    import asyncio

    async def _run():
        async with web._lifespan(web.app):
            pass

    asyncio.run(_run())
    assert called["n"] == 1


def test_llm_calls_endpoint_returns_recent(client):
    from redibis.enrich.llm_logging import build_model_call_record, record_llm_call

    rec = build_model_call_record(
        SimpleNamespace(name="demo", residency="local"),
        "offline-demo",
        None,
        latency_ms=1.0,
        status="ok",
    )
    record_llm_call(rec)

    r = client.get("/api/llm/calls?limit=5")
    assert r.status_code == 200
    calls = r.json()["calls"]
    assert any(c["provider"] == "demo" for c in calls)
