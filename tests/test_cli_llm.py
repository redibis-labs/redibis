"""Tests for ``redibis llm`` CLI and shared ``probe_provider``."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest


def test_normalize_slang_alias():
    from redibis.enrich.probe import normalize_provider_name

    assert normalize_provider_name("slang") == "sglang"
    assert normalize_provider_name("SGLang") == "sglang"
    assert normalize_provider_name("ollama") == "ollama"


def test_probe_demo_offline():
    from redibis.enrich.probe import probe_provider

    result = probe_provider("demo")
    assert result["ok"] is True
    assert result["provider"] == "demo"
    assert result["latency_ms"] >= 0
    assert result.get("response_snippet")


def test_probe_slang_alias_resolves(monkeypatch):
    from redibis.enrich import probe as probe_mod
    from redibis.enrich.providers import LiteLLMProvider

    seen = {}

    class _Stub(LiteLLMProvider):
        def complete(self, system_prompt, user_prompt, *, json_mode=True):
            return "OK"

    def _fake_get(name, **kwargs):
        seen["name"] = name
        return _Stub(name=name, litellm_model="openai/default", api_base="http://localhost:30000/v1")

    monkeypatch.setattr(probe_mod, "get_provider", _fake_get)
    result = probe_mod.probe_provider("slang", endpoint_url="http://localhost:30000/v1")
    assert result["ok"] is True
    assert seen["name"] == "sglang"
    assert result["provider"] == "sglang"


def test_cli_llm_list_includes_sglang(capsys):
    from redibis.cli.llm_cmd import run_llm

    rc = run_llm(SimpleNamespace(llm_action="list", providers_file=None, json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "sglang" in out
    assert "ollama" in out
    assert "vllm" in out


def test_cli_llm_test_demo(capsys):
    from redibis.cli.llm_cmd import run_llm

    rc = run_llm(SimpleNamespace(
        llm_action="test",
        provider="demo",
        all=False,
        include_demo=False,
        model="",
        api_key=None,
        endpoint=None,
        prompt=None,
        timeout=20.0,
        providers_file=None,
        json=False,
        verbose=False,
    ))
    assert rc == 0
    out = capsys.readouterr().out
    assert "[OK] demo" in out


def test_cli_llm_test_json(capsys, monkeypatch):
    from redibis.cli import llm_cmd
    from redibis.enrich.providers import LiteLLMProvider

    class _Stub(LiteLLMProvider):
        def complete(self, system_prompt, user_prompt, *, json_mode=True):
            return "pong"

    monkeypatch.setattr(
        "redibis.enrich.probe.get_provider",
        lambda name, **kw: _Stub(name=name, litellm_model="openai/gpt-4o"),
    )
    rc = llm_cmd.run_llm(SimpleNamespace(
        llm_action="test",
        provider="openai",
        all=False,
        include_demo=False,
        model="gpt-4o-mini",
        api_key="sk-test",
        endpoint=None,
        prompt="ping",
        timeout=5.0,
        providers_file=None,
        json=True,
        verbose=False,
    ))
    assert rc == 0
    import json
    body = json.loads(capsys.readouterr().out)
    assert body["ok"] is True
    assert body["response_snippet"] == "pong"


def test_get_provider_slang_alias():
    from redibis.enrich.providers import get_provider

    p = get_provider("slang", endpoint_url="http://127.0.0.1:30000/v1", api_key="EMPTY")
    assert p.name == "sglang"


def test_local_openai_compat_injects_empty_api_key(monkeypatch):
    """SGLang/openai-compat without a key must still call LiteLLM with a placeholder."""
    from redibis.enrich import providers as prov_mod

    captured = {}

    class _Resp:
        choices = [type("C", (), {"message": type("M", (), {"content": "ok"})()})()]

    def fake_completion(*, model, messages, **kwargs):
        captured["model"] = model
        captured["kwargs"] = kwargs
        return _Resp()

    import litellm
    monkeypatch.setattr(litellm, "completion", fake_completion)

    p = prov_mod.get_provider("sglang", model="Qwen/Qwen2.5-7B-Instruct")
    assert not p.api_key
    out = p.complete("sys", "user", json_mode=False)
    assert out == "ok"
    assert captured["kwargs"].get("api_key") == "EMPTY"
    assert "api_base" in captured["kwargs"]


def test_sglang_in_default_registry():
    from redibis.enrich.providers import list_providers

    names = {p["name"] for p in list_providers()}
    assert "sglang" in names
    assert "vllm" in names
    assert "ollama" in names
