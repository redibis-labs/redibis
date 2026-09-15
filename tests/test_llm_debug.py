"""Tests for LiteLLM call instrumentation and structured logging."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from redibis.enrich.llm_logging import get_recent_llm_calls, redact
from redibis.enrich.providers import LiteLLMProvider
from redibis.telemetry.otel import run_context


class _FakeUsage:
    prompt_tokens = 12
    completion_tokens = 4


class _FakeMessage:
    content = '{"ok": true}'


class _FakeChoice:
    message = _FakeMessage()


class _FakeResp:
    usage = _FakeUsage()
    choices = [_FakeChoice()]


def test_complete_emits_model_call_record_and_log(monkeypatch, caplog):
    import litellm

    def _fake_completion(**kwargs):
        assert "api_key" not in str(kwargs) or kwargs.get("api_key") == "test-key"
        return _FakeResp()

    monkeypatch.setattr(litellm, "completion", _fake_completion)
    monkeypatch.setattr(
        litellm,
        "completion_cost",
        lambda completion_response=None, **kw: 0.001,
    )

    provider = LiteLLMProvider(
        name="openai",
        litellm_model="openai/gpt-4o",
        api_key="test-key",
        extra={"temperature": 0.1},
    )

    with caplog.at_level(logging.INFO, logger="redibis.llm"):
        with run_context("enrich-test"):
            out = provider.complete("sys", "user", json_mode=False)

    assert '"ok": true' in out
    calls = get_recent_llm_calls(limit=5)
    assert calls
    rec = calls[0]
    assert rec["provider"] == "openai"
    assert rec["model_id"] == "openai/gpt-4o"
    assert rec["status"] == "ok"
    assert rec["prompt_tokens"] == 12
    assert rec["completion_tokens"] == 4
    assert rec["latency_ms"] >= 0
    assert rec["system_prompt"] == "sys"
    assert rec["user_prompt"] == "user"
    assert '{"ok": true}' in rec["response"]
    assert "test-key" not in rec["system_prompt"]
    assert "test-key" not in rec["user_prompt"]
    assert "test-key" not in rec["response"]
    assert "test-key" not in caplog.text
    assert any("LLM call provider=openai" in r.message for r in caplog.records)


def test_complete_failure_logs_error_without_api_key(monkeypatch, caplog):
    import litellm

    from redibis.enrich.providers import EnrichmentError

    def _boom(**kwargs):
        raise RuntimeError("auth failed sk-secret123")

    monkeypatch.setattr(litellm, "completion", _boom)

    provider = LiteLLMProvider(
        name="claude",
        litellm_model="anthropic/claude-sonnet-4-6",
        api_key="sk-secret123",
    )

    with caplog.at_level(logging.INFO, logger="redibis.llm"):
        with pytest.raises(EnrichmentError):
            provider.complete("sys", "user", json_mode=False)

    calls = get_recent_llm_calls(limit=1)
    assert calls[0]["status"] == "error"
    assert "sk-secret123" not in calls[0]["error"]
    assert "sk-secret123" not in caplog.text


def test_redact_strips_secrets():
    raw = "Bearer sk-abc123xyz and api_key=sk-live999"
    cleaned = redact(raw)
    assert "sk-abc" not in cleaned
    assert "sk-live" not in cleaned
    assert "[REDACTED]" in cleaned


def test_transcripts_redact_secrets_and_can_be_stripped():
    from types import SimpleNamespace

    from redibis.enrich.llm_logging import build_model_call_record, public_call_dict

    rec = build_model_call_record(
        SimpleNamespace(name="sglang", residency="local"),
        "Qwen/demo",
        None,
        latency_ms=1.0,
        status="ok",
        system_prompt="sys key sk-secret123XYZ",
        user_prompt="user with sk-secret123XYZ",
        response="ok sk-secret123XYZ",
        error_secrets=["sk-secret123XYZ"],
    )
    assert "sk-secret123XYZ" not in rec.system_prompt
    assert "sk-secret123XYZ" not in rec.user_prompt
    assert "sk-secret123XYZ" not in rec.response
    full = public_call_dict(rec, include_transcripts=True)
    assert "system_prompt" in full
    stripped = public_call_dict(rec, include_transcripts=False)
    assert "system_prompt" not in stripped
    assert "user_prompt" not in stripped
    assert "response" not in stripped
    assert stripped["status"] == "ok"
