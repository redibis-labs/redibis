"""Tests for full-contract LLM enrichment with a fake provider (no network)."""

import json
import pytest

from redibis.store.storage_backend import LocalBackend
from redibis.store.contract_store import ContractStore
from redibis.enrich.providers import (
    DemoEnrichmentProvider,
    EnrichmentProvider, LiteLLMProvider, get_provider, list_providers, PROVIDERS,
    normalize_model_id, forbids_sampling_params,
)
from redibis.enrich.service import EnrichmentService, apply_enrichment
from redibis.enrich.diff import build_enrichment_diff


class FakeProvider(EnrichmentProvider):
    def __init__(self, response: dict):
        super().__init__(model="fake-1")
        self.name = "fake"
        self._response = response

    def complete(self, system_prompt, user_prompt, *, json_mode=True):
        return json.dumps(self._response)


@pytest.fixture
def store(tmp_path):
    return ContractStore(LocalBackend(tmp_path / "s"), bucket="active-contracts")


def _seed(store):
    contract = {
        "apiVersion": "v3.0.1", "kind": "DataContract",
        "name": "telecom_customers_contract", "version": "1.0.0", "status": "active",
        "schema": [{
            "name": "telecom_customers", "physicalName": "telecom.customers",
            "properties": [
                {"name": "email", "logicalType": "string", "tags": ["pii"]},
                {"name": "city", "logicalType": "string"},
            ],
        }],
    }
    store.upsert(contract, table="telecom.customers", workflow="manual")


def test_demo_provider_emits_table_definition():
    raw = DemoEnrichmentProvider().complete(
        "sys",
        "# COLUMN EVIDENCE\n\n"
        "table: telecom.merchants\n"
        "physicalName: telecom.merchants\n"
        "columns:\n- name: city\n",
    )
    delta = json.loads(raw)
    assert "telecom.merchants" in delta["table"]["description"]
    assert delta["table"]["purpose"] == "demo enrichment"
    assert "city" in delta["columns"]


def test_apply_enrichment_replaces_business_and_unions_tags():
    active = {"schema": [{"name": "t", "tags": ["x"], "properties": [
        {"name": "email", "tags": ["pii"]},
    ]}]}
    delta = {
        "table_tags": ["telecom"],
        "columns": {"email": {
            "business": {"definition": "Customer email", "synonyms": ["mail"],
                         "example_values": ["a@b.com"], "tags": ["contact"]},
            "pii": {"classification": "pii_personal", "entity_type": "EMAIL_ADDRESS"},
            "tags": ["gdpr"],
        }},
    }
    cand = apply_enrichment(active, delta)
    prop = cand["schema"][0]["properties"][0]
    assert prop["business"]["definition"] == "Customer email"
    assert prop["classification"] == "pii_personal"
    assert prop["entity_type"] == "EMAIL_ADDRESS"
    assert prop["privacy"]["classification"] == "pii_personal"
    assert "pii" in prop["tags"] and "gdpr" in prop["tags"] and "contact" in prop["tags"]
    assert set(cand["schema"][0]["tags"]) == {"x", "telecom"}


def test_apply_enrichment_pii_demotion():
    active = {"schema": [{"name": "t", "properties": [
        {"name": "status", "tags": ["pii"], "classification": "pii_personal",
         "privacy": {"classification_engine": {"detected": True, "entity_type": "CUSTOM"}}},
    ]}]}
    delta = {"columns": {"status": {"pii": {"classification": "none"}}}}
    demotions: list[str] = []
    apply_enrichment(active, delta, pii_demotions=demotions)
    assert demotions == ["status"]


def test_enrich_auto_writes_active(store):
    _seed(store)
    svc = EnrichmentService(store)
    provider = FakeProvider({"columns": {
        "email": {"business": {"definition": "Email address"}}}})
    result = svc.enrich("telecom.customers", provider, run_writer=_run_writer(store))
    assert result.candidate["enrichment_meta"]["provider"] == "fake"
    assert result.candidate["schema"][0]["properties"][0]["business"]["definition"] == "Email address"
    assert result.diff_report["summary"]["adds"] >= 1
    assert result.auto_written
    assert svc.get_candidate("telecom.customers") is None
    active = store.get_active("telecom.customers")
    assert active["schema"][0]["properties"][0]["business"]["definition"] == "Email address"


def _run_writer(store, run_id="test_enrich"):
    from redibis.store.run_output_writer import RunOutputWriter
    return RunOutputWriter(
        backend=store.backend,
        bucket="pii-reports",
        workflow="enrich",
        table="telecom.customers",
        run_id=run_id,
    )


def test_build_enrichment_diff_reports_column_and_table_tags():
    before = {"schema": [{"name": "t", "tags": ["x"], "properties": [
        {"name": "email", "tags": ["pii"], "classification": "pii_personal",
         "privacy": {"classification_engine": {"entity_type": "EMAIL"}}},
        {"name": "city"},
    ]}]}
    after = apply_enrichment(before, {
        "table_tags": ["telecom"],
        "columns": {
            "email": {
                "business": {"definition": "Customer email", "example_values": ["a@b.com"]},
                "pii": {"classification": "pii_personal", "entity_type": "EMAIL_ADDRESS"},
            },
            "city": {"business": {"definition": "City name"}},
        },
    }, pii_demotions=[])
    diff = build_enrichment_diff(before, after)
    assert diff["table_tags"]["added"] == ["telecom"]
    assert diff["summary"]["columns_changed"] == 2
    email_row = next(r for r in diff["columns"] if r["column"] == "email")
    fields = {c["field"] for c in email_row["changes"]}
    assert "business.definition" in fields
    assert "entity_type" in fields


def test_merge_candidate_validity_gate(store):
    _seed(store)
    svc = EnrichmentService(store)
    # Legacy candidate path (manual write for stale hold)
    provider = FakeProvider({"columns": {"city": {"business": {"definition": "City"}}}})
    active = store.get_active("telecom.customers")
    from redibis.enrich.service import apply_enrichment
    candidate = apply_enrichment(active, {"columns": {"city": {"business": {"definition": "City"}}}})
    candidate["enrichment_meta"] = {"enriched_by": "test", "pii_changes": []}
    svc._write_candidate("telecom.customers", candidate)
    result = svc.validate_candidate("telecom.customers")
    if result["valid"]:
        out = svc.merge_candidate("telecom.customers")
        assert out["merged"] is True
        assert svc.get_candidate("telecom.customers") is None
    else:
        with pytest.raises(ValueError):
            svc.merge_candidate("telecom.customers")


def test_context_docs_roundtrip(store):
    _seed(store)
    svc = EnrichmentService(store)
    svc.add_context_doc("telecom.customers", "dict.md", b"# data dictionary\nemail = contact")
    docs = svc.list_context_docs("telecom.customers")
    assert "dict.md" in docs
    assert svc.delete_context_doc("telecom.customers", "dict.md") is True
    assert "dict.md" not in svc.list_context_docs("telecom.customers")


def test_example_docs_roundtrip(store):
    _seed(store)
    svc = EnrichmentService(store)
    svc.add_example_doc("telecom.customers", "good_contract.yaml", b"name: ideal\n")
    assert "good_contract.yaml" in svc.list_example_docs("telecom.customers")
    # example docs are a separate namespace from context docs
    assert "good_contract.yaml" not in svc.list_context_docs("telecom.customers")
    assert svc.delete_example_doc("telecom.customers", "good_contract.yaml") is True


def test_context_and_example_docs_reach_the_prompt(store):
    _seed(store)
    svc = EnrichmentService(store)
    svc.add_context_doc("telecom.customers", "company.md", b"ACME sells telecom plans.")
    svc.add_example_doc("telecom.customers", "ex.yaml", b"name: example_contract")

    captured = {}

    class CaptureProvider(EnrichmentProvider):
        def __init__(self):
            super().__init__(model="cap-1")
            self.name = "cap"
        def complete(self, system_prompt, user_prompt, *, json_mode=True):
            captured["system"] = system_prompt
            captured["user"] = user_prompt
            return json.dumps({"columns": {}})

    svc.enrich("telecom.customers", CaptureProvider(),
               extra_instructions="Use UK English.")
    assert "ACME sells telecom plans." in captured["user"]
    assert "example_contract" in captured["user"]
    assert "Use UK English." in captured["system"]
    assert "ADDITIONAL INSTRUCTIONS" in captured["system"]


def test_sample_data_roundtrip(store):
    _seed(store)
    svc = EnrichmentService(store)
    csv = b"email,city\nfake@example.com,Cairo\n"
    svc.add_sample_data("telecom.customers", "masked.csv", csv)
    assert "masked.csv" in svc.list_sample_data("telecom.customers")
    assert svc.delete_sample_data("telecom.customers", "masked.csv") is True
    assert "masked.csv" not in svc.list_sample_data("telecom.customers")


def test_sample_data_reaches_the_prompt(store):
    _seed(store)
    svc = EnrichmentService(store)
    svc.add_sample_data("telecom.customers", "masked.csv", b"email,city\nx@y.com,Cairo\n")

    captured = {}

    class CaptureProvider(EnrichmentProvider):
        def __init__(self):
            super().__init__(model="cap-1")
            self.name = "cap"
        def complete(self, system_prompt, user_prompt, *, json_mode=True):
            captured["user"] = user_prompt
            return json.dumps({"columns": {}})

    svc.enrich("telecom.customers", CaptureProvider())
    assert "SAMPLE DATA" in captured["user"]
    assert "x@y.com" in captured["user"]
    assert "COLUMN EVIDENCE" in captured["user"]


def test_default_system_prompt_available():
    prompt = EnrichmentService.default_system_prompt()
    assert "ODCS" in prompt
    assert "PII re-review" in prompt
    assert "GOLD STANDARD" in prompt


def test_normalize_model_id_maps_display_names():
    assert normalize_model_id("Claude Opus 4.8", model_prefix="anthropic") == "claude-opus-4-8"
    assert normalize_model_id("claude opus 4.8") == "claude-opus-4-8"
    assert normalize_model_id("anthropic/claude-opus-4-8", model_prefix="anthropic") == "claude-opus-4-8"
    assert normalize_model_id("qwen2.5") == "qwen2.5"


def test_get_provider_normalizes_model_override():
    p = get_provider("claude", model="Claude Opus 4.8")
    assert p._effective_model() == "anthropic/claude-opus-4-8"


def test_forbids_sampling_params_opus_47_48():
    assert forbids_sampling_params("anthropic/claude-opus-4-8")
    assert forbids_sampling_params("claude-opus-4-7")
    assert not forbids_sampling_params("anthropic/claude-opus-4-6")
    assert not forbids_sampling_params("anthropic/claude-sonnet-4-6")


def test_strip_sampling_params_removes_temperature_for_opus_48():
    from redibis.enrich.providers import _strip_sampling_params
    kw = {"temperature": 0.2, "top_p": 0.9, "max_tokens": 4096}
    _strip_sampling_params(kw, "anthropic/claude-opus-4-8")
    assert "temperature" not in kw
    assert "top_p" not in kw
    assert kw["max_tokens"] == 4096


def test_strip_sampling_params_keeps_temperature_for_sonnet_46():
    from redibis.enrich.providers import _strip_sampling_params
    kw = {"temperature": 0.2}
    _strip_sampling_params(kw, "anthropic/claude-sonnet-4-6")
    assert kw["temperature"] == 0.2


def test_get_provider_factory():
    # The packaged JSON registry ships these six LiteLLM-backed providers.
    assert {"vllm", "ollama", "openrouter", "claude", "gemini", "openai"} <= set(PROVIDERS)
    names = {p["name"] for p in list_providers()}
    assert "ollama" in names and "openai" in names

    rows = list_providers()
    # google_genai sorts ahead of LiteLLM gemini when both are registered.
    assert rows[0]["name"] in {"google_genai", "gemini"}
    gemini = next(p for p in rows if p["name"] == "gemini")
    assert gemini["default_model_bare"] == "gemini-3.5-flash"
    ollama = next(p for p in rows if p["name"] == "ollama")
    assert ollama["default_model_bare"] == "llama3.3"
    assert ollama["api_base"] == "http://localhost:11434"
    assert "llama3.3" in ollama["known_models"]

    p = get_provider("vllm", model="qwen2.5", endpoint_url="http://localhost:8000/v1")
    assert isinstance(p, LiteLLMProvider)
    assert p.name == "vllm" and p.model == "qwen2.5"
    # api_base override is honoured, and a bare model is auto-prefixed for LiteLLM.
    assert p.api_base == "http://localhost:8000/v1"
    assert p._effective_model() == "hosted_vllm/qwen2.5"
    with pytest.raises(ValueError):
        get_provider("nope")


def test_provider_uses_default_model_when_no_override():
    p = get_provider("gemini")
    assert p._effective_model() == "gemini/gemini-3.5-flash"


def test_validated_api_base_rejects_key_like_values():
    from redibis.enrich.providers import EnrichmentError, _validated_api_base

    with pytest.raises(EnrichmentError, match="looks like an API key"):
        _validated_api_base("AIzaSyExampleNotARealKey123456789", provider_name="gemini")


def test_validated_api_base_accepts_http_urls():
    from redibis.enrich.providers import _validated_api_base

    assert _validated_api_base("https://gateway.example.com/v1", provider_name="openai") == (
        "https://gateway.example.com/v1"
    )


def test_user_providers_file_extends_registry(tmp_path, monkeypatch):
    cfg = tmp_path / "llm_providers.json"
    cfg.write_text(json.dumps({"providers": {
        "mylocal": {"litellm_model": "ollama/mistral", "model_prefix": "ollama",
                    "api_base": "http://localhost:11434"}}}))
    monkeypatch.setenv("REDIBIS_LLM_PROVIDERS", str(cfg))
    names = {p["name"] for p in list_providers()}
    assert "mylocal" in names and "vllm" in names  # user file extends defaults
    p = get_provider("mylocal")
    assert p._effective_model() == "ollama/mistral"


def test_enrich_body_accepts_null_model_from_ui():
    """v2 enrich tab sends null for empty optional fields — must not 422."""
    from redibis.webapp.backend import EnrichBody

    body = EnrichBody(
        provider="demo",
        model=None,
        endpoint_url=None,
        api_key=None,
        system_prompt="test",
        extra_instructions=None,
        example_contracts=None,
    )
    assert body.model is None
    assert body.provider == "demo"


def test_enrich_body_defaults_to_gemini():
    from redibis.webapp.backend import EnrichBody

    body = EnrichBody(
        model=None,
        endpoint_url=None,
        api_key=None,
        system_prompt="test",
    )
    assert body.provider == "gemini"


def test_get_provider_reroutes_key_like_endpoint_to_api_key():
    p = get_provider(
        "gemini",
        endpoint_url="AIzaSyExampleNotARealKey123456789",
    )
    assert p.api_key == "AIzaSyExampleNotARealKey123456789"
    assert p.api_base is None


def test_get_provider_reroutes_key_like_registry_api_base(monkeypatch, tmp_path):
    cfg = tmp_path / "llm_providers.json"
    cfg.write_text(json.dumps({"providers": {
        "gemini": {
            "litellm_model": "gemini/gemini-2.0-flash",
            "model_prefix": "gemini",
            "api_base": "AIzaSyExampleNotARealKey123456789",
            "api_key_env": "GEMINI_API_KEY",
        }
    }}))
    monkeypatch.setenv("REDIBIS_LLM_PROVIDERS", str(cfg))
    p = get_provider("gemini")
    assert p.api_key == "AIzaSyExampleNotARealKey123456789"
    assert p.api_base is None


def test_lite_llm_provider_reroutes_key_like_api_base_at_complete():
    from unittest.mock import MagicMock, patch

    from redibis.enrich.providers import LiteLLMProvider

    provider = LiteLLMProvider(
        name="gemini",
        litellm_model="gemini/gemini-2.0-flash",
        api_base="AIzaSyExampleNotARealKey123456789",
    )
    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock(message=MagicMock(content='{"ok": true}'))]
    with patch("litellm.completion", return_value=mock_resp) as mock_complete:
        provider.complete("sys", "user", json_mode=False)
    call_kwargs = mock_complete.call_args.kwargs
    assert call_kwargs.get("api_key") == "AIzaSyExampleNotARealKey123456789"
    assert "api_base" not in call_kwargs or not call_kwargs.get("api_base")


def test_enrich_merge_api_roundtrip(tmp_path, monkeypatch):
    """POST /enrich then /enrich/merge persists business definitions."""
    import os
    from fastapi.testclient import TestClient

    root = tmp_path / "storage"
    monkeypatch.setenv("USE_LOCAL_STORAGE", "true")
    monkeypatch.setenv("LOCAL_STORAGE_ROOT", str(root))
    monkeypatch.setenv("SCAN_OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("CONFIGS_DIR", str(tmp_path / "cfg"))

    from redibis.webapp import backend as web

    from redibis.store.config_store import LocalConfigStore
    from redibis.store.contract_store import ContractStore
    from redibis.store.storage_backend import LocalBackend

    store = ContractStore(LocalBackend(str(root)), bucket="active-contracts")
    monkeypatch.setattr(web, "get_contract_store", lambda: store)
    table = "telecom.customers"
    contract = {
        "apiVersion": "v3.0.1", "kind": "DataContract",
        "name": "telecom_customers_contract", "version": "1.0.0", "status": "active",
        "schema": [{
            "name": "telecom_customers", "physicalName": table,
            "properties": [{"name": "city", "logicalType": "string"}],
        }],
    }
    store.upsert(contract, table=table, workflow="manual")

    client = TestClient(web.app)
    enrich = client.post(f"/api/contracts/{table}/enrich", json={"provider": "demo"})
    assert enrich.status_code == 200, enrich.text
    body = enrich.json()
    assert body["valid"] is True
    assert body.get("auto_written") is True

    active = store.get_active(table)
    city = active["schema"][0]["properties"][0]
    assert city["business"]["definition"]

    merge = client.post(f"/api/contracts/{table}/enrich/merge", json={})
    assert merge.status_code == 404
    assert "No enrichment candidate" in merge.json()["detail"]
