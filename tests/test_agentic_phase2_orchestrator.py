"""Phase 2 — Orchestrator, Validator, smart retry."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from redibis.agents.models import PipelineNode
from redibis.agents.orchestrator import (
    ClarificationRequired,
    Orchestrator,
    apply_clarification_to_profile,
    apply_intent_to_spec,
    detect_external_target,
    detect_intent,
)
from redibis.agents.lineage_store import LineageStore
from redibis.agents.planner import heuristic_plan
from redibis.agents.run_models import StepRecord, StepStatus, RunStatus
from redibis.agents.tool_runner import ToolContext
from redibis.agents.validator import (
    ValidationResult,
    apply_critique_to_params,
    should_retry,
    validate_step,
)
from redibis.config import RedibisConfig
from redibis.store.contract_store import ContractStore
from redibis.store.storage_backend import LocalBackend


def test_detect_intent_profile_fields():
    profile = detect_intent("Profile and detect PII on telecom.customers")
    assert "profile" in profile.intents
    assert "pii" in profile.intents
    assert profile.tables == ["telecom.customers"]
    assert profile.target == "local"


def test_detect_intent_enrich_and_external():
    profile = detect_intent("Enrich telecom.customers with gemini descriptions")
    assert "enrich" in profile.intents
    assert profile.target == "external"


def test_apply_intent_sets_enrich_and_target():
    plan = heuristic_plan("enrich telecom.customers")
    profile = detect_intent("enrich telecom.customers")
    profile.intents.append("enrich")
    spec = apply_intent_to_spec(plan.spec, profile)
    contract = next(n for n in spec.nodes if n.kind == "contract")
    assert contract.params.get("enrich") is True
    assert contract.params.get("target") == "local"


def test_validate_step_enrich_odcs(tmp_path):
    store = ContractStore(LocalBackend(tmp_path / "s"), bucket="active-contracts")
    contract = {
        "apiVersion": "v3.0.1",
        "kind": "DataContract",
        "name": "t",
        "version": "1.0.0",
        "schema": [{"name": "s", "properties": []}],
    }
    store.upsert(contract, table="db.t", workflow="manual")
    ctx = ToolContext(contract_store=store, config=RedibisConfig.default())
    node = PipelineNode(kind="enrich", params={"provider": "fake"})
    out = {
        "auto_written": True,
        "valid": True,
        "table": "db.t",
        "enrichment_status": "clean",
    }
    vr = validate_step(node, out, ctx, table="db.t")
    assert vr.ok
    assert vr.checked.get("odcs_valid") is True


def test_validate_step_flags_pii_quality_violation(tmp_path):
    store = ContractStore(LocalBackend(tmp_path / "s"), bucket="active-contracts")
    bad = {
        "apiVersion": "v3.0.1",
        "kind": "DataContract",
        "name": "t",
        "version": "1.0.0",
        "schema": [{
            "name": "s",
            "properties": [{
                "name": "email",
                "classification": "pii_personal",
                "tags": ["pii"],
                "quality": [{"type": "not_null"}],
            }],
        }],
    }
    store.upsert(bad, table="db.t", workflow="manual", validate=False)
    ctx = ToolContext(contract_store=store)
    vr = validate_step(
        PipelineNode(kind="enrich", params={}),
        {"auto_written": True, "valid": False, "table": "db.t"},
        ctx,
        table="db.t",
    )
    assert not vr.ok
    assert vr.severity == "error"
    assert vr.critique


def test_apply_critique_injects_extra_instructions():
    node = PipelineNode(kind="enrich", params={"provider": "fake"})
    vr = ValidationResult(ok=False, severity="error", errors=["missing definition"], critique="Add defs")
    patched = apply_critique_to_params(node, vr)
    assert "VALIDATOR REPAIR" in patched.params["extra_instructions"]
    assert patched.params["repair_critique"] == "Add defs"


def test_should_retry_only_for_critique_kinds():
    step = StepRecord(
        step_id="s1",
        node_id="n1",
        node_kind="enrich",
        tool="t",
        attempts=1,
        max_retries=2,
    )
    node = PipelineNode(kind="enrich", params={})
    vr = ValidationResult(ok=False, severity="error", errors=["x"])
    assert should_retry(step, vr, node) is True
    step.attempts = 2
    assert should_retry(step, vr, node) is True
    step.attempts = 3
    assert should_retry(step, vr, node) is False

    scan = StepRecord(step_id="s2", node_id="n2", node_kind="pii_scan", tool="t", attempts=1, max_retries=2)
    assert should_retry(scan, vr, PipelineNode(kind="pii_scan", params={})) is False


def test_apply_critique_replaces_prior_repair_block():
    node = PipelineNode(kind="enrich", params={"provider": "fake"})
    vr1 = ValidationResult(ok=False, severity="error", errors=["missing definition"], critique="Add defs")
    patched = apply_critique_to_params(node, vr1)
    vr2 = ValidationResult(ok=False, severity="error", errors=["bad odcs"], critique="Fix shape")
    patched2 = apply_critique_to_params(patched, vr2)
    extra = patched2.params["extra_instructions"]
    assert extra.count("# VALIDATOR REPAIR") == 1
    assert "Add defs" not in extra
    assert "Fix shape" in extra


def test_detect_external_target_requires_word_boundary():
    assert detect_external_target("enrich with gemini descriptions") is True
    assert detect_external_target("profile mygemini_table.customers") is False
    assert detect_external_target("local enrich only") is False


def test_apply_clarification_resolves_ambiguous_intent():
    profile = detect_intent("do pii quality enrich")
    assert profile.ambiguous
    resolved = apply_clarification_to_profile(profile, "pii")
    assert resolved.ambiguous is False
    assert "pii" in resolved.intents
    assert "enrich" not in resolved.intents


def test_orchestrator_run_with_clarification_proceeds(tmp_path):
    from redibis.agents.run_models import AgentRun

    lineage = LineageStore(tmp_path / "runs")
    ctx = ToolContext(config=RedibisConfig.default())
    orch = Orchestrator(lineage=lineage, tool_ctx=ctx)
    finished = AgentRun(
        run_id="r1",
        name="test",
        status=RunStatus.COMPLETED,
        pipeline={"name": "test", "nodes": [], "edges": []},
        tables=["schema.table"],
    )
    with patch.object(orch, "execute", return_value=finished):
        profile, plan, run = orch.run("do pii quality enrich", clarification="pii")
    assert profile.ambiguous is False
    assert "pii" in profile.intents
    assert run.run_id == "r1"


def test_orchestrator_run_raises_clarification_when_ambiguous(tmp_path):
    lineage = LineageStore(tmp_path / "runs")
    ctx = ToolContext(config=RedibisConfig.default())
    orch = Orchestrator(lineage=lineage, tool_ctx=ctx)
    with pytest.raises(ClarificationRequired) as exc:
        orch.run("do pii quality enrich")
    assert "workflow" in exc.value.question.lower()


def test_orchestrator_plan_heuristic_offline(tmp_path):
    lineage = MagicMock()
    lineage.root = tmp_path / "runs"
    ctx = ToolContext(config=RedibisConfig.default())
    orch = Orchestrator(lineage=lineage, tool_ctx=ctx)
    profile = detect_intent("PII scan telecom.customers")
    result = orch.plan("PII scan telecom.customers", profile)
    assert result.valid
    assert any(n.kind == "contract" for n in result.spec.nodes)
