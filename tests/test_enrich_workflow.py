"""Tests for enrichment workflow YAML and table-description delta."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from redibis.enrich.delta_schema import filter_delta_for_stage, parse_enrichment_delta
from redibis.enrich.diff import build_enrichment_diff
from redibis.enrich.service import _table_prompt_meta, apply_enrichment
from redibis.enrich.workflow import default_workflow, load_workflow_file, parse_workflow


def test_parse_enrichment_delta_table_fields():
    delta, errors = parse_enrichment_delta({
        "table": {"description": "Customer master table."},
        "table_tags": ["telecom"],
        "columns": {
            "msisdn": {"business": {"definition": "Mobile number"}},
        },
    })
    assert errors == []
    assert delta["table"]["description"] == "Customer master table."
    assert delta["table_tags"] == ["telecom"]


def test_table_prompt_meta_reads_existing_description():
    meta = _table_prompt_meta({
        "description": {"description": "Top-level narrative.", "purpose": "registry"},
        "schema": [{
            "description": "Schema narrative.",
            "purpose": "schema purpose",
            "tags": ["telecom"],
            "properties": [],
        }],
    })
    assert meta["existing_description"] == "Schema narrative."
    assert meta["existing_purpose"] == "schema purpose"
    assert meta["table_tags"] == ["telecom"]


def test_apply_enrichment_writes_table_description():
    active = {
        "apiVersion": "v3.0.1",
        "kind": "DataContract",
        "name": "telecom_customers_contract",
        "version": "1.0.0",
        "status": "active",
        "physicalName": "telecom.customers",
        "schema": [{
            "name": "telecom_customers",
            "physicalName": "telecom.customers",
            "description": "old",
            "properties": [{"name": "msisdn", "logicalType": "string"}],
        }],
    }
    candidate = apply_enrichment(active, {
        "table": {"description": "One row per subscriber at activation."},
        "columns": {},
    })
    assert candidate["schema"][0]["description"] == "One row per subscriber at activation."
    diff = build_enrichment_diff(active, candidate)
    assert diff["table_description"]["after"] == "One row per subscriber at activation."


def test_filter_delta_for_stage():
    delta = {
        "table": {"description": "T"},
        "table_tags": ["x"],
        "columns": {
            "a": {
                "business": {"definition": "A"},
                "pii": {"classification": "none"},
            },
        },
    }
    cols = filter_delta_for_stage(delta, "column_definitions")
    assert "pii" not in cols["columns"]["a"]
    assert "business" in cols["columns"]["a"]
    pii = filter_delta_for_stage(delta, "classification_pii")
    assert "pii" in pii["columns"]["a"]
    assert "business" not in pii["columns"]["a"]
    table = filter_delta_for_stage(delta, "table_definition")
    assert table["table"]["description"] == "T"
    assert table["columns"] == {}
    review = filter_delta_for_stage(delta, "contract_review")
    assert review["table"]["description"] == "T"
    assert "business" in review["columns"]["a"]
    assert "pii" in review["columns"]["a"]


def test_workflow_yaml_add_remove_reorder(tmp_path):
    path = tmp_path / "steps.yaml"
    path.write_text(
        yaml.safe_dump({
            "version": 1,
            "max_attempts": 5,
            "rai_enabled": False,
            "steps": [
                {"id": "table", "kind": "table_definition", "enabled": True},
                {"id": "columns", "kind": "column_definitions", "enabled": True},
                {"id": "privacy", "kind": "classification_pii", "enabled": False},
            ],
        }),
        encoding="utf-8",
    )
    wf = load_workflow_file(path)
    assert wf.max_attempts == 5
    assert wf.rai_enabled is False
    enabled = wf.enabled_steps()
    assert [s.id for s in enabled] == ["table", "columns"]


def test_workflow_rejects_unknown_kind():
    with pytest.raises(ValueError, match="unknown workflow step kind"):
        parse_workflow({
            "version": 1,
            "steps": [{"id": "x", "kind": "not_a_real_stage", "enabled": True}],
        })


def test_default_workflow_has_four_stages():
    wf = default_workflow()
    assert [s.kind for s in wf.enabled_steps()] == [
        "column_definitions", "classification_pii", "table_definition", "contract_review",
    ]


def test_example_steps_file_loads():
    example = (
        Path(__file__).resolve().parents[1]
        / "config" / "examples" / "enrich-steps-default.yaml"
    )
    wf = load_workflow_file(example)
    assert len(wf.enabled_steps()) == 4
    assert wf.enabled_steps()[-1].kind == "contract_review"


def _seed_contract(store):
    from redibis.contracts.privacy import apply_privacy_to_column

    email = {"name": "email", "logicalType": "string", "tags": ["pii"]}
    apply_privacy_to_column(email, {
        "classification": "pii",
        "pii": {"detected": True, "entity_type": "EMAIL_ADDRESS"},
    })
    contract = {
        "apiVersion": "v3.0.1",
        "kind": "DataContract",
        "name": "telecom_customers_contract",
        "version": "1.0.0",
        "status": "active",
        "schema": [{
            "name": "telecom_customers",
            "physicalName": "telecom.customers",
            "description": "old",
            "properties": [
                email,
                {"name": "city", "logicalType": "string"},
            ],
        }],
    }
    store.upsert(contract, table="telecom.customers", workflow="manual", run_id="seed")
    return contract


def test_multistep_one_final_write_and_pii_demotion(tmp_path, monkeypatch):
    import json

    from redibis.config import RAIConfig, RedibisConfig
    from redibis.enrich.multistep import MultistepEnrichmentRunner
    from redibis.enrich.providers import EnrichmentProvider
    from redibis.enrich.service import EnrichmentService
    from redibis.enrich.workflow import EnrichmentStep, EnrichmentWorkflow
    from redibis.store.contract_store import ContractStore
    from redibis.store.storage_backend import LocalBackend

    monkeypatch.setattr(
        "redibis.enrich.multistep.langgraph_available", lambda: False,
    )

    class FakeProvider(EnrichmentProvider):
        def __init__(self, response: dict):
            super().__init__(model="fake-1")
            self.name = "fake"
            self._response = response
            self.calls = 0

        def complete(self, system_prompt, user_prompt, *, json_mode=True):
            self.calls += 1
            return json.dumps(self._response)

    store = ContractStore(LocalBackend(tmp_path / "s"), bucket="active-contracts")
    _seed_contract(store)
    before = len(store.get_history("telecom.customers"))

    provider = FakeProvider({
        "columns": {
            "email": {
                "business": {"definition": "Subscriber email"},
                "pii": {"classification": "none"},
            },
            "city": {"business": {"definition": "City of residence"}},
        },
        "table": {"description": "One row per telecom customer."},
        "table_tags": ["telecom"],
    })
    svc = EnrichmentService(store)
    wf = EnrichmentWorkflow(
        max_attempts=1,
        rai_enabled=False,
        steps=[
            EnrichmentStep(id="columns", kind="column_definitions"),
            EnrichmentStep(id="privacy", kind="classification_pii"),
            EnrichmentStep(id="table", kind="table_definition"),
        ],
    )
    runner = MultistepEnrichmentRunner(
        svc, provider, workflow=wf,
        redibis_config=RedibisConfig(rai=RAIConfig(enabled=False)),
        bypass_rai=True,
        run_id="ms1",
    )
    result = runner.run("telecom.customers")
    assert result.auto_written is True
    assert result.valid is True
    assert provider.calls == 3
    # One enrichment upsert via _apply_active_write; PII demotion may add a
    # separate overlay history entry (same as single-shot enrich).
    history = store.get_history("telecom.customers")
    assert len(history) >= before + 1
    assert any(getattr(h, "workflow", None) == "business" for h in history)

    active = store.get_active("telecom.customers")
    assert active["schema"][0]["description"] == "One row per telecom customer."
    decisions = store.get_pii_decisions("telecom.customers")
    assert decisions.get("email", {}).get("status") == "not_pii"


def test_multistep_stops_after_failed_stage(tmp_path, monkeypatch):
    import json

    from redibis.config import RAIConfig, RedibisConfig
    from redibis.enrich.multistep import MultistepEnrichmentRunner
    from redibis.enrich.providers import EnrichmentProvider
    from redibis.enrich.service import EnrichmentService
    from redibis.enrich.workflow import EnrichmentStep, EnrichmentWorkflow
    from redibis.store.contract_store import ContractStore
    from redibis.store.storage_backend import LocalBackend

    # Force sequential path for deterministic call counting; LangGraph path
    # uses the same _execute_step + conditional edges.
    monkeypatch.setattr(
        "redibis.enrich.multistep.langgraph_available", lambda: False,
    )

    class EmptyProvider(EnrichmentProvider):
        def __init__(self):
            super().__init__(model="fake-1")
            self.name = "fake"
            self.calls = 0

        def complete(self, system_prompt, user_prompt, *, json_mode=True):
            self.calls += 1
            return json.dumps({"columns": {}})

    store = ContractStore(LocalBackend(tmp_path / "s"), bucket="active-contracts")
    _seed_contract(store)
    before = len(store.get_history("telecom.customers"))
    provider = EmptyProvider()
    svc = EnrichmentService(store)
    wf = EnrichmentWorkflow(
        max_attempts=1,
        rai_enabled=False,
        steps=[
            EnrichmentStep(id="columns", kind="column_definitions"),
            EnrichmentStep(id="table", kind="table_definition"),
        ],
    )
    runner = MultistepEnrichmentRunner(
        svc, provider, workflow=wf,
        redibis_config=RedibisConfig(rai=RAIConfig(enabled=False)),
        bypass_rai=True,
        run_id="ms-fail",
    )
    result = runner.run("telecom.customers")
    assert result.auto_written is False
    assert result.valid is False
    assert provider.calls == 1  # second stage must not run
    assert len(store.get_history("telecom.customers")) == before


def test_langgraph_stops_after_failed_stage(tmp_path):
    import json

    pytest.importorskip("langgraph")

    from redibis.config import RAIConfig, RedibisConfig
    from redibis.enrich.multistep import MultistepEnrichmentRunner
    from redibis.enrich.providers import EnrichmentProvider
    from redibis.enrich.service import EnrichmentService
    from redibis.enrich.workflow import EnrichmentStep, EnrichmentWorkflow
    from redibis.store.contract_store import ContractStore
    from redibis.store.storage_backend import LocalBackend

    class EmptyProvider(EnrichmentProvider):
        def __init__(self):
            super().__init__(model="fake-1")
            self.name = "fake"
            self.calls = 0

        def complete(self, system_prompt, user_prompt, *, json_mode=True):
            self.calls += 1
            return json.dumps({"columns": {}})

    store = ContractStore(LocalBackend(tmp_path / "s"), bucket="active-contracts")
    _seed_contract(store)
    provider = EmptyProvider()
    svc = EnrichmentService(store)
    wf = EnrichmentWorkflow(
        max_attempts=1,
        rai_enabled=False,
        steps=[
            EnrichmentStep(id="columns", kind="column_definitions"),
            EnrichmentStep(id="table", kind="table_definition"),
        ],
    )
    runner = MultistepEnrichmentRunner(
        svc, provider, workflow=wf,
        redibis_config=RedibisConfig(rai=RAIConfig(enabled=False)),
        bypass_rai=True,
        run_id="ms-lg",
    )
    result = runner.run("telecom.customers")
    assert result.auto_written is False
    assert provider.calls == 1


def test_langgraph_invoke_failure_does_not_duplicate_llm_calls(tmp_path, monkeypatch):
    """A LangGraph runtime error after some stages ran must finalize with the
    partial progress already made, not restart the whole workflow (which would
    re-call the LLM for stages that already succeeded)."""
    import json

    from redibis.config import RAIConfig, RedibisConfig
    from redibis.enrich.multistep import MultistepEnrichmentRunner
    from redibis.enrich.providers import EnrichmentProvider
    from redibis.enrich.service import EnrichmentService
    from redibis.enrich.workflow import EnrichmentStep, EnrichmentWorkflow
    from redibis.store.contract_store import ContractStore
    from redibis.store.storage_backend import LocalBackend

    class FakeProvider(EnrichmentProvider):
        def __init__(self, response: dict):
            super().__init__(model="fake-1")
            self.name = "fake"
            self._response = response
            self.calls = 0

        def complete(self, system_prompt, user_prompt, *, json_mode=True):
            self.calls += 1
            return json.dumps(self._response)

    store = ContractStore(LocalBackend(tmp_path / "s"), bucket="active-contracts")
    _seed_contract(store)
    provider = FakeProvider({
        "columns": {"city": {"business": {"definition": "City of residence"}}},
    })
    svc = EnrichmentService(store)
    wf = EnrichmentWorkflow(
        max_attempts=1,
        rai_enabled=False,
        steps=[
            EnrichmentStep(id="columns", kind="column_definitions"),
            EnrichmentStep(id="table", kind="table_definition"),
        ],
    )
    runner = MultistepEnrichmentRunner(
        svc, provider, workflow=wf,
        redibis_config=RedibisConfig(rai=RAIConfig(enabled=False)),
        bypass_rai=True,
        run_id="ms-lg-fail",
    )

    # Exercise the LangGraph branch of run() even if the langgraph package is
    # not installed in this environment — _build_langgraph/_invoke_langgraph
    # are mocked below so no real graph is built.
    monkeypatch.setattr(
        "redibis.enrich.multistep.langgraph_available", lambda: True,
    )
    monkeypatch.setattr(runner, "_build_langgraph", lambda: "fake-compiled")

    def fake_invoke(compiled, table):
        assert compiled == "fake-compiled"
        seed = runner._seed_candidate(table)
        node = runner._langgraph_node(wf.enabled_steps()[0])
        state = node({
            "table": table, "c_det": seed, "candidate": seed,
            "stages": [], "errors": [], "done": False,
        })
        runner._last_state = state
        raise RuntimeError("simulated langgraph runtime error")

    monkeypatch.setattr(runner, "_invoke_langgraph", fake_invoke)

    result = runner.run("telecom.customers")
    assert provider.calls == 1  # only the first stage's LLM call happened
    assert result.auto_written is False
    assert any("langgraph invocation error" in e for e in result.errors)
    assert len(runner.stage_records) == 1
    assert runner.stage_records[0].ok is True


def test_multistep_forwards_context_reduction_approval(tmp_path, monkeypatch):
    """CLI-approved pack context reduction must reach every stage's enrich() call."""
    import json

    from redibis.config import RAIConfig, RedibisConfig
    from redibis.enrich.multistep import MultistepEnrichmentRunner
    from redibis.enrich.providers import EnrichmentProvider
    from redibis.enrich.service import EnrichmentService
    from redibis.enrich.workflow import EnrichmentStep, EnrichmentWorkflow
    from redibis.store.contract_store import ContractStore
    from redibis.store.storage_backend import LocalBackend

    monkeypatch.setattr(
        "redibis.enrich.multistep.langgraph_available", lambda: False,
    )

    class FakeProvider(EnrichmentProvider):
        def __init__(self, response: dict):
            super().__init__(model="fake-1")
            self.name = "fake"
            self._response = response

        def complete(self, system_prompt, user_prompt, *, json_mode=True):
            return json.dumps(self._response)

    store = ContractStore(LocalBackend(tmp_path / "s"), bucket="active-contracts")
    _seed_contract(store)
    provider = FakeProvider({
        "columns": {"city": {"business": {"definition": "City of residence"}}},
    })
    svc = EnrichmentService(store)
    wf = EnrichmentWorkflow(
        max_attempts=1,
        rai_enabled=False,
        steps=[EnrichmentStep(id="columns", kind="column_definitions")],
    )
    runner = MultistepEnrichmentRunner(
        svc, provider, workflow=wf,
        redibis_config=RedibisConfig(rai=RAIConfig(enabled=False)),
        bypass_rai=True,
        run_id="ms-approve",
        approve_context_reduction="plan-123",
        reduction_approval_source="interactive_cli",
        reduction_approved_by="alice",
    )

    captured: dict = {}
    real_enrich = svc.enrich

    def spy_enrich(*args, **kwargs):
        captured.update(kwargs)
        return real_enrich(*args, **kwargs)

    monkeypatch.setattr(svc, "enrich", spy_enrich)

    runner.run("telecom.customers")
    assert captured.get("approve_context_reduction") == "plan-123"
    assert captured.get("reduction_approval_source") == "interactive_cli"
    assert captured.get("reduction_approved_by") == "alice"


def test_review_enabled_false_drops_default_review_stage():
    from redibis.config import EnrichConfig, EnrichMultistepConfig, RedibisConfig
    from redibis.enrich.multistep import MultistepEnrichmentRunner
    from redibis.enrich.providers import EnrichmentProvider
    from redibis.enrich.service import EnrichmentService
    from redibis.store.contract_store import ContractStore
    from redibis.store.storage_backend import LocalBackend

    cfg = RedibisConfig(
        enrich=EnrichConfig(multistep=EnrichMultistepConfig(review_enabled=False)),
    )
    store = ContractStore(LocalBackend("/tmp/unused"), bucket="active-contracts")
    svc = EnrichmentService(store)
    provider = EnrichmentProvider(model="x")
    runner = MultistepEnrichmentRunner.from_config(svc, provider, redibis_config=cfg)
    assert [s.kind for s in runner.workflow.enabled_steps()] == [
        "column_definitions", "classification_pii", "table_definition",
    ]


def test_contract_review_empty_delta_succeeds(tmp_path, monkeypatch):
    import json

    from redibis.config import RAIConfig, RedibisConfig
    from redibis.enrich.multistep import MultistepEnrichmentRunner
    from redibis.enrich.providers import EnrichmentProvider
    from redibis.enrich.service import EnrichmentService
    from redibis.enrich.workflow import EnrichmentStep, EnrichmentWorkflow
    from redibis.store.contract_store import ContractStore
    from redibis.store.storage_backend import LocalBackend

    monkeypatch.setattr("redibis.enrich.multistep.langgraph_available", lambda: False)

    class ReviewProvider(EnrichmentProvider):
        def __init__(self):
            super().__init__(model="fake-1")
            self.name = "fake"
            self.calls = 0

        def complete(self, system_prompt, user_prompt, *, json_mode=True):
            self.calls += 1
            return json.dumps({"review": {"findings": []}})

    store = ContractStore(LocalBackend(tmp_path / "s"), bucket="active-contracts")
    _seed_contract(store)
    svc = EnrichmentService(store)
    wf = EnrichmentWorkflow(
        max_attempts=2,
        rai_enabled=False,
        steps=[EnrichmentStep(id="review", kind="contract_review")],
    )
    runner = MultistepEnrichmentRunner(
        svc, ReviewProvider(), workflow=wf,
        redibis_config=RedibisConfig(rai=RAIConfig(enabled=False)),
        bypass_rai=True,
        run_id="ms-review-empty",
    )
    result = runner.run("telecom.customers")
    assert result.valid is True
    assert result.auto_written is True
    assert runner.stage_records[0].ok is True
    assert runner.stage_records[0].delta.get("columns") in ({}, None)
    assert runner.stage_records[0].attempt == 1


def test_contract_review_corrects_and_records_findings(tmp_path, monkeypatch):
    import json

    from redibis.config import RAIConfig, RedibisConfig
    from redibis.enrich.multistep import MultistepEnrichmentRunner
    from redibis.enrich.providers import EnrichmentProvider
    from redibis.enrich.service import EnrichmentService
    from redibis.enrich.workflow import EnrichmentStep, EnrichmentWorkflow
    from redibis.store.contract_store import ContractStore
    from redibis.store.storage_backend import LocalBackend

    monkeypatch.setattr("redibis.enrich.multistep.langgraph_available", lambda: False)

    class ReviewProvider(EnrichmentProvider):
        def __init__(self):
            super().__init__(model="fake-1")
            self.name = "fake"

        def complete(self, system_prompt, user_prompt, *, json_mode=True):
            return json.dumps({
                "review": {
                    "findings": [{
                        "severity": "warning",
                        "target": "column:email",
                        "message": "definition contradicts PII classification",
                    }],
                },
                "columns": {
                    "email": {
                        "business": {"definition": "Subscriber email address used for notices."},
                        "pii": {"classification": "pii_personal", "entity_type": "EMAIL_ADDRESS",
                                "reason": "clear email identifier"},
                    },
                },
            })

    store = ContractStore(LocalBackend(tmp_path / "s"), bucket="active-contracts")
    _seed_contract(store)
    svc = EnrichmentService(store)
    wf = EnrichmentWorkflow(
        max_attempts=1,
        rai_enabled=False,
        steps=[EnrichmentStep(id="review", kind="contract_review")],
    )
    runner = MultistepEnrichmentRunner(
        svc, ReviewProvider(), workflow=wf,
        redibis_config=RedibisConfig(rai=RAIConfig(enabled=False)),
        bypass_rai=True,
        run_id="ms-review-fix",
    )
    result = runner.run("telecom.customers")
    assert result.valid is True
    rec = runner.stage_records[0]
    assert rec.ok is True
    assert rec.review_findings
    assert rec.review_findings[0]["target"] == "column:email"
    assert "review" not in (result.candidate or {})
    assert result.enrichment_meta.get("review_findings")
    active = store.get_active("telecom.customers")
    assert "review" not in active
    email = next(p for p in active["schema"][0]["properties"] if p["name"] == "email")
    assert "Subscriber email" in (email.get("business") or {}).get("definition", "")


def test_contract_review_max_changes_cap(tmp_path, monkeypatch):
    import json

    from redibis.config import (
        EnrichConfig,
        EnrichMultistepConfig,
        RAIConfig,
        RedibisConfig,
    )
    from redibis.enrich.multistep import MultistepEnrichmentRunner
    from redibis.enrich.providers import EnrichmentProvider
    from redibis.enrich.service import EnrichmentService
    from redibis.enrich.workflow import EnrichmentStep, EnrichmentWorkflow
    from redibis.store.contract_store import ContractStore
    from redibis.store.storage_backend import LocalBackend

    monkeypatch.setattr("redibis.enrich.multistep.langgraph_available", lambda: False)

    class ReviewProvider(EnrichmentProvider):
        def __init__(self):
            super().__init__(model="fake-1")
            self.name = "fake"

        def complete(self, system_prompt, user_prompt, *, json_mode=True):
            return json.dumps({
                "columns": {
                    "email": {"business": {"definition": "A"}},
                    "city": {"business": {"definition": "B"}},
                },
            })

    store = ContractStore(LocalBackend(tmp_path / "s"), bucket="active-contracts")
    _seed_contract(store)
    svc = EnrichmentService(store)
    wf = EnrichmentWorkflow(
        max_attempts=1,
        rai_enabled=False,
        steps=[EnrichmentStep(id="review", kind="contract_review")],
    )
    cfg = RedibisConfig(
        rai=RAIConfig(enabled=False),
        enrich=EnrichConfig(multistep=EnrichMultistepConfig(review_max_changes=1)),
    )
    runner = MultistepEnrichmentRunner(
        svc, ReviewProvider(), workflow=wf,
        redibis_config=cfg,
        bypass_rai=True,
        run_id="ms-review-cap",
    )
    result = runner.run("telecom.customers")
    assert result.valid is True
    rec = runner.stage_records[0]
    assert len(rec.delta.get("columns") or {}) == 1
    assert any("review_max_changes" in f.get("message", "") for f in rec.review_findings)

