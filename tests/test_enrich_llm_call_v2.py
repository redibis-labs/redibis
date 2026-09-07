"""Plan A — Enrichment LLM Call v2 acceptance tests."""

import json
import zipfile
from pathlib import Path

import pytest
import yaml

from redibis.enrich.delta_schema import parse_enrichment_delta
from redibis.enrich.providers import EnrichmentProvider
from redibis.enrich.service import EnrichmentService, enrichment_service_for_store
from redibis.store.contract_store import ContractStore
from redibis.store.run_output_writer import RunOutputWriter
from redibis.store.storage_backend import LocalBackend


class FakeProvider(EnrichmentProvider):
    def __init__(self, response: dict, *, name: str = "fake"):
        super().__init__(model="fake-1")
        self.name = name
        self._response = response

    def complete(self, system_prompt, user_prompt, *, json_mode=True):
        return json.dumps(self._response)


@pytest.fixture
def store(tmp_path):
    return ContractStore(LocalBackend(tmp_path / "s"), bucket="active-contracts")


def _seed_contract(store):
    contract = {
        "apiVersion": "v3.0.1",
        "kind": "DataContract",
        "name": "merchants_contract",
        "version": "1.0.0",
        "status": "active",
        "schema": [{
            "name": "merchants",
            "physicalName": "telecom.merchants",
            "properties": [
                {
                    "name": "merchant_mobile",
                    "logicalType": "string",
                    "physicalType": "string",
                    "classification": "pii_personal",
                    "tags": ["pii", "gdpr_personal_data"],
                    "entity_type": "PHONE_NUMBER",
                    "privacy": {
                        "classification": "pii_personal",
                        "classification_engine": {
                            "entity_type": "PHONE_NUMBER",
                            "confidence": 0.97,
                        },
                    },
                },
                {"name": "city", "logicalType": "string", "physicalType": "string"},
            ],
        }],
    }
    store.upsert(contract, table="telecom.merchants", workflow="manual")
    store.metadata.merge_column_telemetry("telecom.merchants", {
        "merchant_mobile": {
            "entity_type": "PHONE_NUMBER",
            "confidence": 0.97,
            "sample": ["01007746235", "01118889999"],
            "null_rate": 0.0,
            "unique_ratio": 0.99,
            "min_len": 11,
            "max_len": 11,
        },
        "city": {
            "null_rate": 0.02,
            "distinct_count": 120,
            "sample": ["Cairo", "Alexandria", "Giza"],
        },
    })
    return contract


def test_profiling_populated_for_all_columns(store):
    from redibis.enrich.evidence import build_full_column_context

    _seed_contract(store)
    tel = store.metadata.get_column_telemetry("telecom.merchants")
    props = store.get_active("telecom.merchants")["schema"][0]["properties"]
    for prop in props:
        ctx = build_full_column_context(
            prop, tel.get(prop["name"], {}), sample_policy="raw",
        )
        profiling = ctx.get("profiling") or {}
        assert profiling, f"expected profiling for {prop['name']}"
        assert "logicalType" in profiling

    mobile = next(p for p in props if p["name"] == "merchant_mobile")
    mobile_ctx = build_full_column_context(
        mobile, tel["merchant_mobile"], sample=tel["merchant_mobile"]["sample"],
    )
    mp = mobile_ctx["profiling"]
    assert mp["min_len"] == 11
    assert mp["char_classes"]["digit_share"] >= 0.99
    assert mp["top_formats"]


def test_local_run_includes_gold_example_and_samples(store):
    _seed_contract(store)
    svc = EnrichmentService(store)
    ctx = svc.build_context("telecom.merchants", FakeProvider({}))
    assert ctx.sample_policy == "raw"
    mobile = next(c for c in ctx.column_context if c["name"] == "merchant_mobile")
    assert mobile.get("sample")
    assert "__gold__" in ctx.example_contracts
    assert "EXAMPLE CONTRACTS" in ctx.user_prompt


def test_cloud_run_masks_samples_no_gold(store):
    _seed_contract(store)
    svc = EnrichmentService(store)
    ctx = svc.build_context("telecom.merchants", FakeProvider({}, name="gemini"))
    assert ctx.sample_policy == "masked"
    assert "__gold__" not in ctx.example_contracts
    mobile = next(c for c in ctx.column_context if c["name"] == "merchant_mobile")
    assert "sample" not in mobile


def test_prompt_context_artifact_includes_pack_digest(store):
    _seed_contract(store)
    svc = EnrichmentService(store)
    run_id = "ctx_run"
    writer = RunOutputWriter(
        backend=store.backend, bucket="pii-reports",
        workflow="enrich", table="telecom.merchants", run_id=run_id,
    )
    provider = FakeProvider({
        "columns": {
            "merchant_mobile": {"business": {"definition": "Mobile"}},
            "city": {"business": {"definition": "City"}},
        },
    })
    svc.enrich("telecom.merchants", provider, run_writer=writer, run_id=run_id)
    payload = store.backend.get_json(
        "pii-reports", f"enrich/telecom_merchants/{run_id}/llm_prompt_context.json",
    )
    assert payload["docs"]["pack_digest"]
    mobile = next(
        c for c in payload["contract_for_prompt"]["columns"]
        if c["name"] == "merchant_mobile"
    )
    assert mobile.get("deterministic_reasoning")


def test_contract_llm_yaml_has_no_enrichment_meta(store):
    _seed_contract(store)
    svc = EnrichmentService(store)
    run_id = "meta_run"
    writer = RunOutputWriter(
        backend=store.backend, bucket="pii-reports",
        workflow="enrich", table="telecom.merchants", run_id=run_id,
    )
    provider = FakeProvider({
        "columns": {
            "merchant_mobile": {"business": {"definition": "Mobile"}},
            "city": {"business": {"definition": "City"}},
        },
    })
    result = svc.enrich("telecom.merchants", provider, run_writer=writer, run_id=run_id)
    assert result.candidate.get("enrichment_meta")
    llm_doc = store.backend.get_yaml(
        "pii-reports", f"enrich/telecom_merchants/{run_id}/contract.llm.yaml",
    )
    assert "enrichment_meta" not in llm_doc
    meta_doc = store.backend.get_json(
        "pii-reports", f"enrich/telecom_merchants/{run_id}/enrichment_meta.json",
    )
    assert meta_doc["provider"] == "fake"


def test_enrich_run_writes_all_six_artifacts(store):
    _seed_contract(store)
    svc = EnrichmentService(store)
    run_id = "artifact_run"
    writer = RunOutputWriter(
        backend=store.backend, bucket="pii-reports",
        workflow="enrich", table="telecom.merchants", run_id=run_id,
    )
    provider = FakeProvider({
        "columns": {
            "merchant_mobile": {"business": {"definition": "Mobile"}},
            "city": {"business": {"definition": "City"}},
        },
    })
    svc.enrich("telecom.merchants", provider, run_writer=writer, run_id=run_id)
    prefix = f"enrich/telecom_merchants/{run_id}"
    for name in (
        "contract.deterministic.yaml",
        "contract.llm.yaml",
        "llm_prompt_context.json",
        "contract_diff.json",
        "contract_diff.md",
        "enrichment_meta.json",
        "telecom.merchants.debug.log",
    ):
        assert store.backend.exists("pii-reports", f"{prefix}/{name}"), name


def test_delta_validation_drops_unknown_keys():
    delta, errors = parse_enrichment_delta({
        "table_tags": ["telecom"],
        "unexpected": True,
        "columns": {
            "email": {
                "business": {"definition": "Email"},
                "extra_field": "drop me",
            },
        },
    })
    assert "email" in delta.get("columns", {})
    assert any("unknown" in e for e in errors)


def test_pii_prompt_encourages_active_participation():
    from redibis.enrich.service import DEFAULT_SYSTEM_PROMPT

    assert "false positives and false" in DEFAULT_SYSTEM_PROMPT.lower()
    assert "negatives" in DEFAULT_SYSTEM_PROMPT.lower()
    assert "rubber-stamp" in DEFAULT_SYSTEM_PROMPT.lower()


def test_cli_get_llm_call_logs_zip(store, tmp_path, monkeypatch):
    _seed_contract(store)
    svc = EnrichmentService(store)
    run_id = "cli_zip_run"
    writer = RunOutputWriter(
        backend=store.backend, bucket="pii-reports",
        workflow="enrich", table="telecom.merchants", run_id=run_id,
    )
    provider = FakeProvider({
        "columns": {
            "merchant_mobile": {"business": {"definition": "Mobile"}},
            "city": {"business": {"definition": "City"}},
        },
    })
    svc.enrich("telecom.merchants", provider, run_writer=writer, run_id=run_id)

    from redibis.cli.get_cmd import run_get

    class Args:
        get_action = "llm-call-logs"
        run_or_session_id = run_id
        out = None
        zip = str(tmp_path / "bundle.zip")

    assert run_get(Args(), store, store.backend) == 0
    with zipfile.ZipFile(tmp_path / "bundle.zip") as zf:
        names = set(zf.namelist())
    assert "contract.llm.yaml" in names
    assert "enrichment_meta.json" in names
    assert "llm_prompt_context.json" in names


def test_ui_and_agent_identical_prompts(store):
    _seed_contract(store)
    provider = FakeProvider({})
    web_ctx = enrichment_service_for_store(store).build_context("telecom.merchants", provider)
    agent_ctx = enrichment_service_for_store(store).build_context("telecom.merchants", provider)
    assert web_ctx.system_prompt == agent_ctx.system_prompt
    assert web_ctx.contract_for_prompt == agent_ctx.contract_for_prompt
