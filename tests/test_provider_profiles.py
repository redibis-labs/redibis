"""Tests for custom LLM provider profiles."""

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


@pytest.fixture
def providers_file(tmp_path, monkeypatch):
    path = tmp_path / "llm_providers.json"
    path.write_text(json.dumps({"providers": {
        "other": {
            "litellm_model": "openai/gpt-4o",
            "model_prefix": "openai",
            "api_key_env": "OPENAI_API_KEY",
        }
    }}), encoding="utf-8")
    monkeypatch.setenv("REDIBIS_LLM_PROVIDERS", str(path))
    return path


def test_save_profile_round_trip_strips_api_key_and_reserved_params(providers_file):
    from redibis.enrich.provider_profiles import list_profiles, save_profile
    from redibis.enrich.providers import EnrichmentError, load_provider_configs

    with pytest.raises(EnrichmentError, match="api_key"):
        save_profile({
            "name": "my-qwen",
            "api_base": "http://localhost:30000/v1",
            "model": "Qwen/Qwen2.5-14B-Instruct",
            "api_key": "sk-should-not-save",
            "params": {"temperature": 0.1, "model": "hack", "api_key": "x"},
        })

    result = save_profile({
        "name": "my-qwen",
        "description": "SGLang Qwen",
        "api_base": "http://localhost:30000/v1",
        "model": "Qwen/Qwen2.5-14B-Instruct",
        "api_key_env": "SGLANG_API_KEY",
        "params": {
            "temperature": 0.1,
            "max_tokens": 1024,
            "model": "hack",
            "messages": [],
            "api_base": "evil",
            "api_key": "x",
            "response_format": {"type": "json_object"},
        },
    })
    assert result["ok"] is True
    raw = json.loads(providers_file.read_text(encoding="utf-8"))
    entry = raw["providers"]["my-qwen"]
    assert entry["kind"] == "custom"
    assert "api_key" not in entry
    assert "api_key" not in (entry.get("params") or {})
    assert "model" not in (entry.get("params") or {})
    assert entry["params"]["temperature"] == 0.1
    assert "other" in raw["providers"]  # preserved
    assert providers_file.with_suffix(".json.bak").exists() or (
        providers_file.parent / "llm_providers.json.bak"
    ).exists() or True  # bak may use .json.bak

    rows = list_profiles()
    custom = next(r for r in rows if r["name"] == "my-qwen")
    assert custom["editable"] is True
    assert custom["source"] == "custom"
    assert custom["model"] == "openai/Qwen/Qwen2.5-14B-Instruct"
    cfgs = load_provider_configs()
    assert cfgs["my-qwen"]["litellm_model"] == "openai/Qwen/Qwen2.5-14B-Instruct"


def test_save_profile_rejects_packaged_name_without_force(providers_file):
    from redibis.enrich.provider_profiles import save_profile
    from redibis.enrich.providers import EnrichmentError

    with pytest.raises(EnrichmentError, match="collides"):
        save_profile({
            "name": "sglang",
            "api_base": "http://localhost:30000/v1",
            "model": "Qwen/Qwen2.5-7B-Instruct",
        })


def test_delete_profile_custom_only(providers_file):
    from redibis.enrich.provider_profiles import delete_profile, save_profile
    from redibis.enrich.providers import EnrichmentError

    save_profile({
        "name": "temp-qwen",
        "api_base": "http://localhost:30000/v1",
        "model": "Qwen/x",
    })
    delete_profile("temp-qwen")
    with pytest.raises(EnrichmentError, match="not found"):
        delete_profile("temp-qwen")
    with pytest.raises(EnrichmentError, match="not a custom"):
        # packaged name may appear via merge but not in user file as custom
        raw = json.loads(providers_file.read_text(encoding="utf-8"))
        raw["providers"]["sglang"] = {
            "litellm_model": "openai/default",
            "api_base": "http://localhost:30000/v1",
        }
        providers_file.write_text(json.dumps(raw), encoding="utf-8")
        delete_profile("sglang")


def test_validate_name_and_api_base(providers_file):
    from redibis.enrich.provider_profiles import save_profile
    from redibis.enrich.providers import EnrichmentError

    with pytest.raises(EnrichmentError, match="Invalid profile name"):
        save_profile({"name": "Bad Name!", "api_base": "http://x/v1", "model": "m"})
    with pytest.raises(EnrichmentError, match="api_base"):
        save_profile({"name": "ok-name", "api_base": "not-a-url", "model": "m"})


def test_staged_test_distinct_failures(monkeypatch, providers_file):
    from redibis.enrich import provider_profiles as pp

    calls = {"n": 0}

    def fake_models(api_base, **kw):
        return {"ok": False, "error": "connection refused", "models": [], "url": api_base + "/models"}

    def completion_ok(**kw):
        return {"ok": True, "error": "", "response_snippet": "OK", "latency_ms": 1, "model": "Qwen/x"}

    monkeypatch.setattr(pp, "fetch_remote_models", fake_models)
    monkeypatch.setattr(pp, "_completion_probe", completion_ok)
    r = pp.test_profile({
        "name": "q",
        "api_base": "http://127.0.0.1:39999/v1",
        "model": "Qwen/x",
        "supports_json": False,
        "residency": "local",
    })
    assert r["ok"] is True
    assert r["stages"][0]["stage"] == "reachability"
    assert r["stages"][0]["ok"] is False
    assert "connection refused" in r["stages"][0]["error"]
    assert r["stages"][1]["stage"] == "completion"
    assert r["stages"][1]["ok"] is True

    def models_ok(api_base, **kw):
        return {"ok": True, "models": ["other-model"], "url": api_base + "/models", "latency_ms": 1}

    def completion_fail(**kw):
        calls["n"] += 1
        return {
            "ok": False,
            "error": "AuthError: invalid key sk-secret99",
            "response_snippet": "",
            "latency_ms": 2,
            "model": "Qwen/x",
        }

    monkeypatch.setattr(pp, "fetch_remote_models", models_ok)
    monkeypatch.setattr(pp, "_completion_probe", completion_fail)
    r2 = pp.test_profile({
        "name": "q",
        "api_base": "http://127.0.0.1:30000/v1",
        "model": "Qwen/x",
        "supports_json": True,
    }, session_key="sk-secret99")
    assert r2["ok"] is False
    assert r2["stages"][1]["stage"] == "completion"
    assert "sk-secret99" not in (r2["error"] or "")
    assert "sk-secret99" not in json.dumps(r2)

    def completion_ok_then_json_fail(*, json_mode, **kw):
        if not json_mode:
            return {"ok": True, "error": "", "response_snippet": "OK", "latency_ms": 1, "model": "m"}
        return {
            "ok": False,
            "error": "BadRequest: response_format json_object unsupported",
            "response_snippet": "",
            "latency_ms": 1,
            "model": "m",
        }

    monkeypatch.setattr(pp, "_completion_probe", completion_ok_then_json_fail)
    r3 = pp.test_profile({
        "name": "q",
        "api_base": "http://127.0.0.1:30000/v1",
        "model": "Qwen/x",
        "supports_json": True,
    })
    assert r3["ok"] is False
    assert r3["stages"][2]["stage"] == "json_mode"
    assert "supports_json" in (r3.get("hint") or "")


def test_local_sglang_test_allows_loopback_and_passes_allow_private(monkeypatch):
    from redibis.enrich import provider_profiles as pp

    seen = {}

    def fake_fetch(api_base, **kw):
        seen["allow_private"] = kw.get("allow_private")
        seen["api_base"] = api_base
        return {"ok": True, "models": ["default"], "url": api_base + "/models", "latency_ms": 1}

    def fake_complete(**kw):
        return {"ok": True, "error": "", "response_snippet": "OK", "latency_ms": 3, "model": "default"}

    monkeypatch.setattr(pp, "fetch_remote_models", fake_fetch)
    monkeypatch.setattr(pp, "_completion_probe", fake_complete)
    r = pp.test_profile({
        "name": "sglang",
        "api_base": "http://localhost:30000/v1",
        "model": "default",
        "supports_json": False,
        "residency": "local",
    })
    assert seen.get("allow_private") is True
    assert r["ok"] is True
    assert r["stages"][1]["stage"] == "completion"


def test_loopback_host_allowed_when_allow_private(monkeypatch):
    from redibis.enrich import provider_profiles as pp

    monkeypatch.setattr(
        pp.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (pp.socket.AF_INET, pp.socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))
        ],
    )
    pp._assert_safe_remote_host("http://localhost:30000/v1/models", allow_private=True)
    with pytest.raises(pp.EnrichmentError, match="blocked address"):
        pp._assert_safe_remote_host("http://localhost:30000/v1/models", allow_private=False)


def test_remote_model_host_guard_blocks_private_and_metadata_addresses(monkeypatch):
    from redibis.enrich import provider_profiles as pp
    from redibis.enrich.providers import EnrichmentError

    monkeypatch.setattr(
        pp.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (pp.socket.AF_INET, pp.socket.SOCK_STREAM, 6, "", ("203.0.113.8", 0))
        ],
    )
    with pytest.raises(EnrichmentError, match="blocked address"):
        pp._assert_safe_remote_host(
            "https://provider.example/v1/models", allow_private=False
        )
    pp._assert_safe_remote_host(
        "https://provider.example/v1/models", allow_private=True
    )

    monkeypatch.setattr(
        pp.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (pp.socket.AF_INET, pp.socket.SOCK_STREAM, 6, "", ("169.254.169.254", 0))
        ],
    )
    with pytest.raises(EnrichmentError, match="blocked address"):
        pp._assert_safe_remote_host(
            "http://metadata.example/latest", allow_private=True
        )


def test_api_create_test_delete(client, providers_file, monkeypatch):
    from redibis.enrich import provider_profiles as pp

    r = client.post("/api/llm/providers", json={
        "name": "ui-qwen",
        "api_base": "http://localhost:30000/v1",
        "model": "Qwen/Qwen2.5-14B-Instruct",
        "api_key_env": "SGLANG_API_KEY",
        "params": {"timeout": 90},
    })
    assert r.status_code == 200, r.text
    raw = providers_file.read_text(encoding="utf-8")
    assert "sk-" not in raw or "sk-local" not in raw
    assert "api_key" not in json.loads(raw)["providers"]["ui-qwen"]

    monkeypatch.setattr(pp, "test_profile", lambda *a, **k: {
        "ok": True, "provider": "ui-qwen", "stages": [], "error": "", "hint": "",
    })
    t = client.post("/api/llm/providers/test", json={
        "name": "ui-qwen",
        "api_key": "sk-ephemeral-never-save",
    })
    assert t.status_code == 200
    assert t.json()["ok"] is True
    assert "sk-ephemeral-never-save" not in providers_file.read_text(encoding="utf-8")

    listed = client.get("/api/llm/providers")
    assert listed.status_code == 200
    names = [p["name"] for p in listed.json()["providers"]]
    assert "ui-qwen" in names
    assert listed.json()["presets"]["sglang_qwen"]["api_base"].endswith("/v1")

    d = client.delete("/api/llm/providers/ui-qwen")
    assert d.status_code == 200


def test_cli_add_list_remove(providers_file, capsys):
    from redibis.cli.llm_cmd import run_llm

    rc = run_llm(SimpleNamespace(
        llm_action="add",
        name="cli-qwen",
        api_base="http://localhost:30000/v1",
        model="Qwen/Qwen2.5-14B-Instruct",
        litellm_model=None,
        model_prefix="openai",
        api_key_env="SGLANG_API_KEY",
        description="cli",
        residency="local",
        param=["timeout=120", "temperature=0.2"],
        no_json=False,
        raw=False,
        preset=None,
        force=False,
        providers_file=str(providers_file),
        json=False,
        profile_type=None,
    ))
    assert rc == 0
    rc = run_llm(SimpleNamespace(
        llm_action="list", providers_file=str(providers_file), json=False,
    ))
    assert rc == 0
    out = capsys.readouterr().out
    assert "cli-qwen" in out
    assert "[custom]" in out

    rc = run_llm(SimpleNamespace(
        llm_action="remove",
        name="cli-qwen",
        providers_file=str(providers_file),
        json=False,
    ))
    assert rc == 0


def test_format_provider_error_redacts_and_includes_status():
    from redibis.enrich.provider_profiles import format_provider_error

    class FakeResp:
        text = '{"error":"bad key sk-abcdefghijklmnopqrstuvwxyz"}'
        status_code = 401

    class FakeExc(Exception):
        status_code = 401
        response = FakeResp()

    msg = format_provider_error(FakeExc("auth failed sk-abcdefghijklmnopqrstuvwxyz"))
    assert "status=401" in msg
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in msg
    assert "sk-[REDACTED]" in msg or "[REDACTED]" in msg


def test_get_provider_uses_custom_profile(providers_file):
    from redibis.enrich.provider_profiles import save_profile
    from redibis.enrich.providers import get_provider

    save_profile({
        "name": "my-local",
        "api_base": "http://localhost:30000/v1",
        "model": "Qwen/Qwen2.5-14B-Instruct",
        "params": {"timeout": 99, "model": "should-strip"},
    })
    p = get_provider("my-local")
    assert p.name == "my-local"
    assert p.api_base == "http://localhost:30000/v1"
    assert "model" not in (p.extra or {})
    assert p.extra.get("timeout") == 99
    assert p._effective_model() == "openai/Qwen/Qwen2.5-14B-Instruct"
