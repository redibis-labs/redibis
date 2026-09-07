"""FIX 1: LLM log redaction for all provider key formats."""

from __future__ import annotations

from redibis.enrich.llm_logging import redact


def test_redact_google_anthropic_azure_and_literal_secret():
    literal_key = "supersecretliteralkey99"
    raw = (
        f"google key AIzaSyDUMMYKEY1234567890abcdef "
        f"x-api-key: abc123secret "
        f"literal {literal_key}"
    )
    cleaned = redact(raw, secrets=[literal_key])
    assert "AIzaSyDUMMYKEY" not in cleaned
    assert "abc123secret" not in cleaned
    assert literal_key not in cleaned
    assert "[REDACTED]" in cleaned


def test_redact_xai_gsk_and_sk_ant_prefixes():
    raw = "xai-deadbeef99 and gsk_live_token_xyz and sk-ant-api03-abcdefghij"
    cleaned = redact(raw)
    assert "xai-deadbeef" not in cleaned
    assert "gsk_live" not in cleaned
    assert "sk-ant-api" not in cleaned


def test_redact_api_key_header():
    raw = 'api-key: "azure-secret-value-here"'
    cleaned = redact(raw)
    assert "azure-secret" not in cleaned
    assert "api-key=[REDACTED]" in cleaned
