"""Phase 4 — Google Gen AI provider selection and graceful fallback."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def providers_file(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "llm_providers.json"
    path.write_text(
        json.dumps({
            "providers": {
                "google_genai": {
                    "kind": "google_genai",
                    "default_model": "gemini-2.5-flash",
                    "api_key_env": "GEMINI_API_KEY",
                    "supports_json": True,
                    "params": {"temperature": 0.1},
                },
                "gemini": {
                    "litellm_model": "gemini/gemini-2.5-flash",
                    "model_prefix": "gemini",
                    "api_key_env": "GEMINI_API_KEY",
                },
            }
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("REDIBIS_LLM_PROVIDERS", str(path))
    return path


def test_get_provider_selects_google_genai(providers_file, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    from redibis.enrich.google_genai_provider import GoogleGenAIProvider
    from redibis.enrich.providers import get_provider

    p = get_provider("google_genai", model="gemini-2.5-flash")
    assert isinstance(p, GoogleGenAIProvider)
    assert p._effective_model() == "gemini-2.5-flash"


def test_get_provider_alias_google_genai(providers_file, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    from redibis.enrich.google_genai_provider import GoogleGenAIProvider
    from redibis.enrich.providers import get_provider

    p = get_provider("genai")
    assert isinstance(p, GoogleGenAIProvider)


def test_google_genai_complete_mocked(providers_file, monkeypatch):
    from redibis.enrich.google_genai_provider import GoogleGenAIProvider

    class _Resp:
        text = '{"service":"ranger","table":"t","policies":[]}'

    class _Models:
        def generate_content(self, **kwargs):
            assert kwargs["model"] == "gemini-2.5-flash"
            return _Resp()

    class _Client:
        models = _Models()

    prov = GoogleGenAIProvider(api_key="k", default_model="gemini-2.5-flash")
    monkeypatch.setattr(prov, "_build_client", lambda: _Client())
    out = prov.complete("sys", "user", json_mode=True)
    assert "ranger" in out


def test_google_genai_missing_sdk_raises(monkeypatch):
    from redibis.enrich.google_genai_provider import GoogleGenAIProvider
    from redibis.enrich.providers import EnrichmentError

    import builtins

    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "google" or (isinstance(name, str) and name.startswith("google.")):
            raise ImportError("blocked")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    with pytest.raises(EnrichmentError, match="google-genai"):
        GoogleGenAIProvider(api_key="k")._build_client()
