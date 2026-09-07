"""Tests for Text Gateway LLM refiner routing (capability role + SGLang)."""

from __future__ import annotations

import pytest

from redibis.pii.text_llm import LlmTextRefiner, _LOCAL_LLM_PROVIDERS
from redibis.pii.scan.result import TextScanConfig, Candidate


def test_sglang_in_local_allowlist():
    assert "sglang" in _LOCAL_LLM_PROVIDERS
    assert "vllm" in _LOCAL_LLM_PROVIDERS
    assert "ollama" in _LOCAL_LLM_PROVIDERS


def test_sglang_allowed_by_default():
    refiner = LlmTextRefiner()
    refiner._assert_local_provider("sglang")  # must not raise


def test_resolve_provider_uses_capability_role(monkeypatch):
    class FakeProv:
        name = "sglang"
        model = "Qwen/Qwen2.5-7B-Instruct"

        def complete(self, system, user, **kw):
            return '{"spans":[]}'

    class FakeBinding:
        provider = "sglang"
        model = "Qwen/Qwen2.5-7B-Instruct"
        enabled = True

    def fake_role(*a, **k):
        return FakeProv(), FakeBinding()

    monkeypatch.setattr(
        "redibis.enrich.capability_routing.get_provider_for_role", fake_role
    )
    monkeypatch.setattr(
        "redibis.config.load_global_settings_optional", lambda: {}
    )

    refiner = LlmTextRefiner()
    prov, model_id = refiner._resolve_provider()
    assert prov.name == "sglang"
    assert "Qwen" in model_id


def test_arabic_system_prompt_used(monkeypatch):
    captured = {}

    class FakeProv:
        name = "sglang"
        model = "qwen"

        def complete(self, system, user, **kw):
            captured["system"] = system
            return '{"spans":[{"start":0,"end":4,"entity_type":"PERSON","score":0.9}]}'

    refiner = LlmTextRefiner(provider=FakeProv())
    monkeypatch.setattr(
        "redibis.telemetry.model_gateway.guarded_model_call",
        lambda fn, **kw: (fn(), None),
    )
    text = "أحمد هنا"
    hits = refiner.propose_spans(
        text, [], TextScanConfig(use_llm=True, language="ar", arabic=True)
    )
    assert "Arabic" in captured["system"] or "Egyptian" in captured["system"]
    assert hits and hits[0].is_proposal
    assert text[hits[0].start:hits[0].end] == hits[0].text


def test_service_attaches_refiner_when_role_bound(monkeypatch):
    from redibis.config import RedibisConfig
    from redibis.services.text_pii_service import TextPIIService

    class FakeProv:
        name = "sglang"
        model = "qwen"

    class FakeBinding:
        provider = "sglang"
        model = "qwen"
        enabled = True

    monkeypatch.setattr(
        "redibis.config.load_global_settings_optional", lambda: {}
    )
    monkeypatch.setattr(
        "redibis.enrich.capability_routing.get_provider_for_role",
        lambda *a, **k: (FakeProv(), FakeBinding()),
    )
    # Avoid NER load
    monkeypatch.setattr(
        TextPIIService, "_try_load_ner", lambda self: None
    )

    cfg = RedibisConfig()
    cfg.pii.llm.enabled = False
    svc = TextPIIService(redibis_config=cfg)
    assert svc._llm is not None
    health = svc.health()
    assert health["engines"]["llm"]["role_bound"] is True
    assert health["engines"]["preprocess"]["available"] is True
