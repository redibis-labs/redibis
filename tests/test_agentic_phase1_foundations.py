"""Phase 1 foundations — always-active enrich + auditable governance state."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from redibis.agents import BatchExecutor, PipelineSpec, ToolContext
from redibis.agents.agent_report import (
    build_agent_report,
    compute_enrichment_status,
    render_agent_report_md,
)
from redibis.agents.dag_trace import build_audit_dag
from redibis.agents.governance_state import GovernanceState, stable_hash
from redibis.agents.lineage_store import LineageStore
from redibis.agents.models import PipelineNode
from redibis.agents.run_log import (
    append_audit_event,
    drop_run_log,
    load_audit_events,
    persist_audit_events,
    snapshot_audit_events,
)
from redibis.agents.run_models import RunStatus, StepRecord, StepStatus
from redibis.agents.validator import (
    _defer_enrich_write,
    _node_defers_enrich_write,
    run_step_with_validation,
)
from redibis.config import RedibisConfig
from redibis.enrich.delta_schema import assert_odcs_v3_contract
from redibis.enrich.providers import EnrichmentProvider
from redibis.enrich.service import EnrichmentService
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
                    "tags": ["pii"],
                    "entity_type": "PHONE_NUMBER",
                },
                {"name": "city", "logicalType": "string", "physicalType": "string"},
            ],
        }],
    }
    store.upsert(contract, table="telecom.merchants", workflow="manual")
    return contract


def test_compute_enrichment_status_levels():
    assert compute_enrichment_status(
        valid=True, errors=[], warnings=[], delta_errors=[], scrubbed_cols=[],
    ) == "clean"
    assert compute_enrichment_status(
        valid=True, errors=[], warnings=["w"], delta_errors=[], scrubbed_cols=[],
    ) == "warn"
    assert compute_enrichment_status(
        valid=False, errors=["e"], warnings=[], delta_errors=[], scrubbed_cols=[],
    ) == "degraded"


def test_enrich_always_writes_active_even_when_invalid(store):
    _seed_contract(store)
    svc = EnrichmentService(store)
    run_id = "always_active"
    writer = RunOutputWriter(
        backend=store.backend,
        bucket="pii-reports",
        workflow="enrich",
        table="telecom.merchants",
        run_id=run_id,
    )
    provider = FakeProvider({
        "columns": {
            "merchant_mobile": {"business": {"definition": "Mobile line"}},
            "city": {"business": {"definition": "City name"}},
        },
    })

    real_validate = store.validate

    def _invalid_validate(doc, strict=False):
        out = real_validate(doc, strict=strict)
        return {**out, "valid": False, "errors": ["simulated ODCS failure"]}

    with patch.object(store, "validate", side_effect=_invalid_validate):
        result = svc.enrich(
            "telecom.merchants",
            provider,
            run_writer=writer,
            run_id=run_id,
        )

    assert result.auto_written is True
    assert result.enrichment_status == "degraded"
    assert store.get_active("telecom.merchants") is not None
    tel = store.metadata.get_telemetry("telecom.merchants")
    assert tel.get("enrichment_status") == "degraded"
    report = store.backend.get_json(
        "pii-reports",
        f"enrich/telecom_merchants/{run_id}/agent_report.json",
    )
    assert report["enrichment_status"] == "degraded"
    assert report["review_first"] is not None
    md_key = f"enrich/telecom_merchants/{run_id}/agent_report.md"
    assert store.backend.exists("pii-reports", md_key)


def test_agent_report_has_rerun_handles(store):
    c_det = _seed_contract(store)
    c_llm = store.get_active("telecom.merchants")
    report = build_agent_report(
        table="telecom.merchants",
        run_id="r1",
        c_det=c_det,
        c_llm=c_llm,
        valid=True,
        errors=[],
        warnings=[],
        enrichment_status="clean",
        diff_report={"fields": []},
        enrichment_meta={"system_prompt_hash": "abc123"},
    )
    md = render_agent_report_md(report)
    assert "Review these first" in md or "Per-column" in md
    for col in report["columns"]:
        assert col["rerun_handle"]["table"] == "telecom.merchants"
        assert col["rerun_handle"]["action"] == "enrich"


def test_governance_state_records_transitions():
    state = GovernanceState(run_id="r1", table="db.t", status="pending")
    next_state = state.transition(field="status", value="running", node="profile")
    assert len(next_state.transitions) == 1
    assert next_state.transitions[0].field == "status"
    again = next_state.transition(field="status", value="running", node="profile")
    assert len(again.transitions) == 1


def test_governance_state_graph_bridge():
    gov = GovernanceState(run_id="r1", table="db.t", node_index=0, status="running")
    graph = gov.to_graph_state()
    synced = GovernanceState.from_graph_state(
        {**graph, "node_index": 1, "status": "completed"},
        run_id="r1",
        base=gov,
    )
    assert synced.node_index == 1
    assert synced.status == "completed"
    assert synced.transitions


def test_audit_events_append_only_and_persist(tmp_path):
    rid = "audit-run-1"
    lineage = tmp_path / "runs"
    try:
        append_audit_event(
            rid,
            table="db.t",
            node="enrich",
            actor="agent",
            inputs_hash=stable_hash({"a": 1}),
            outputs_hash=stable_hash({"b": 2}),
            status="completed",
            validation={"valid": True, "enrichment_status": "clean"},
        )
        events = snapshot_audit_events(rid)
        assert len(events) == 1
        assert events[0]["actor"] == "agent"
        path = persist_audit_events(lineage, rid)
        assert path is not None and path.is_file()
        loaded = load_audit_events(lineage, rid)
        assert len(loaded) == 1
    finally:
        drop_run_log(rid)


def test_build_audit_dag_includes_audit_events(tmp_path):
    from redibis.agents.run_models import AgentRun, RunStatus

    rid = "dag-audit-1"
    lineage = tmp_path / "runs"
    append_audit_event(rid, table="t", node="enrich", status="completed")
    persist_audit_events(lineage, rid)
    run = AgentRun(run_id=rid, status=RunStatus.COMPLETED, tables=["t"])
    audit = build_audit_dag(run, lineage_root=lineage)
    assert "audit_events" in audit
    assert len(audit["audit_events"]) == 1


def test_provenance_records_prompt_hash(store):
    _seed_contract(store)
    svc = EnrichmentService(store)
    provider = FakeProvider({
        "columns": {
            "merchant_mobile": {"business": {"definition": "Mobile"}},
            "city": {"business": {"definition": "City"}},
        },
    })
    svc.enrich("telecom.merchants", provider, run_id="prov_run")
    prov = store.metadata.get_provenance("telecom.merchants")
    assert prov
    last = prov[-1]
    assert last.get("prompt_hash")
    assert last.get("llm_version")
    assert last.get("active_source") == "llm"


def test_active_contract_has_no_telemetry_keys_after_enrich(store):
    """Regression: always-active write must not leak telemetry into active spec."""
    _seed_contract(store)
    svc = EnrichmentService(store)
    run_id = "telemetry_clean"
    writer = RunOutputWriter(
        backend=store.backend,
        bucket="pii-reports",
        workflow="enrich",
        table="telecom.merchants",
        run_id=run_id,
    )
    provider = FakeProvider({
        "columns": {
            "merchant_mobile": {"business": {"definition": "Mobile line"}},
            "city": {"business": {"definition": "City name"}},
        },
    })
    svc.enrich("telecom.merchants", provider, run_writer=writer, run_id=run_id)
    active = store.get_active("telecom.merchants")
    assert active is not None
    forbidden = (
        "enrichment_meta",
        "provenance",
        "pii_summary",
        "last_updated",
        "last_updated_by_workflow",
        "_scan_metadata",
    )
    for key in forbidden:
        assert key not in active, f"{key!r} leaked into active contract"
    odcs_errors = assert_odcs_v3_contract(active)
    assert not any("telemetry key" in e for e in odcs_errors)


def test_validator_degraded_keeps_step_completed(store):
    """Invalid enrich output: contract stays active; step is degraded, not failed."""
    _seed_contract(store)
    node = PipelineNode(id="e1", kind="enrich", label="Enrich", params={"provider": "fake"})
    ctx = ToolContext(contract_store=store, config=RedibisConfig.default())
    bad_output = {
        "auto_written": True,
        "valid": False,
        "missing_definitions": 2,
        "enrichment_status": "degraded",
        "table": "telecom.merchants",
        "run_id": "deg_run",
        "validation_reported": True,
        "artifact_odcs_errors": ["simulated ODCS failure"],
        "validation_errors": ["simulated ODCS failure"],
    }
    completed = StepRecord(
        step_id="s1",
        node_id="e1",
        node_kind="enrich",
        tool="enrich",
        status=StepStatus.COMPLETED,
        output=bad_output,
    )
    with patch("redibis.agents.validator.run_node_step", return_value=completed):
        step = run_step_with_validation(
            node, table="telecom.merchants", ctx=ctx, max_retries=0,
        )
    assert step.status == StepStatus.COMPLETED
    assert step.output.get("degraded") is True
    assert step.output.get("enrichment_status") == "degraded"
    assert store.get_active("telecom.merchants") is not None


def test_enrich_defer_write_commits_once(store):
    """Agent retry path: defer active upsert until commit_active_write."""
    _seed_contract(store)
    svc = EnrichmentService(store)
    provider = FakeProvider({
        "columns": {
            "merchant_mobile": {"business": {"definition": "Mobile line"}},
            "city": {"business": {"definition": "City name"}},
        },
    })
    before = len(store.get_history("telecom.merchants"))
    deferred = svc.enrich(
        "telecom.merchants",
        provider,
        run_id="defer_run",
        write_active=False,
    )
    assert deferred.auto_written is False
    assert deferred.deferred_context
    assert len(store.get_history("telecom.merchants")) == before
    committed = svc.commit_active_write(deferred)
    assert committed.auto_written is True
    assert len(store.get_history("telecom.merchants")) == before + 1


def test_defer_enrich_write_applies_to_contract_node():
    contract = PipelineNode(
        kind="contract",
        params={"enrich": True, "provider": "fake", "pii": False, "quality": False},
    )
    assert _node_defers_enrich_write(contract) is True
    assert _defer_enrich_write(attempt=1, max_retries=2, node=contract) is True
    assert _defer_enrich_write(attempt=3, max_retries=2, node=contract) is False

    scan = PipelineNode(kind="pii_scan", params={})
    assert _node_defers_enrich_write(scan) is False


def test_contract_enrich_defer_commits_once(store):
    """Contract+enrich path defers active write during retries, commits once at end."""
    from redibis.agents.scan_bindings import get_table_state

    _seed_contract(store)
    svc = EnrichmentService(store)
    provider = FakeProvider({
        "columns": {
            "merchant_mobile": {"business": {"definition": "Mobile line"}},
            "city": {"business": {"definition": "City name"}},
        },
    })
    before = len(store.get_history("telecom.merchants"))
    pending = svc.enrich(
        "telecom.merchants",
        provider,
        run_id="contract_defer",
        write_active=False,
    )
    assert pending.auto_written is False
    assert len(store.get_history("telecom.merchants")) == before

    node = PipelineNode(
        id="c1",
        kind="contract",
        label="Contract",
        params={"enrich": True, "provider": "fake", "pii": False, "quality": False, "classify": False},
    )
    ctx = ToolContext(contract_store=store, config=RedibisConfig.default())
    get_table_state(ctx.run_states, "telecom.merchants").extras["pending_enrich"] = pending

    deferred_sub = {
        "kind": "enrich",
        "auto_written": False,
        "deferred_write": True,
        "valid": pending.valid,
        "validation_reported": True,
        "artifact_odcs_errors": [],
        "safety_errors": [],
        "enrichment_status": pending.enrichment_status,
        "table": "telecom.merchants",
        "run_id": "contract_defer",
    }
    completed = StepRecord(
        step_id="s1",
        node_id="c1",
        node_kind="contract",
        tool="contract",
        status=StepStatus.COMPLETED,
        output={"sub_steps": [deferred_sub], "run_id": "contract_defer"},
    )

    with patch("redibis.agents.validator.run_node_step", return_value=completed):
        step = run_step_with_validation(
            node, table="telecom.merchants", ctx=ctx, max_retries=0,
        )

    assert len(store.get_history("telecom.merchants")) == before + 1
    enrich_sub = next(s for s in step.output["sub_steps"] if s["kind"] == "enrich")
    assert enrich_sub["auto_written"] is True
    assert enrich_sub["deferred_write"] is False


def test_validate_enrich_uses_service_report_not_active_reload(store):
    """Validator trusts enrich step output instead of re-loading active contract."""
    from redibis.agents.validator import validate_step

    _seed_contract(store)
    node = PipelineNode(kind="enrich", params={})
    ctx = ToolContext(contract_store=store, config=RedibisConfig.default())
    output = {
        "auto_written": True,
        "valid": True,
        "validation_reported": True,
        "artifact_odcs_errors": [],
        "safety_errors": [],
        "enrichment_status": "clean",
    }
    with patch.object(store, "get_active", side_effect=AssertionError("should not reload active")):
        vr = validate_step(node, output, ctx, table="telecom.merchants")
    assert vr.ok


def test_sequential_batch_records_governance_state(tmp_path):
    """Phase 1 Ruling B: sequential batch backend must persist governance transitions."""
    lineage = LineageStore(tmp_path / "runs")
    cfg = RedibisConfig.default()
    cfg.agents.batch_executor = "sequential"
    ctx = ToolContext(config=cfg)
    spec = PipelineSpec(
        name="gov-seq",
        nodes=[PipelineNode(kind="source_table", label="Src", params={"table": "db.t"})],
        edges=[],
    )
    executor = BatchExecutor(lineage, tool_ctx=ctx)
    run = executor.run(spec, tables=["db.t"], dry_run=True)
    assert run.status == RunStatus.COMPLETED
    gov = lineage.load_governance_state(run.run_id)
    assert gov is not None
    assert gov.get("table") == "db.t"
    assert gov.get("transitions")
    assert gov.get("steps")
