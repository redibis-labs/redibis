"""Tests for LangGraph executor + agentic session (Phase 3)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from redibis.agents.executor import LangGraphExecutor
from redibis.agents.lineage_store import LineageStore
from redibis.agents.models import PipelineEdge, PipelineNode, PipelineSpec
from redibis.agents.pipeline_executor import langgraph_available
from redibis.agents.registry import validate_spec
from redibis.agents.run_models import RunStatus, StepStatus
from redibis.agents.session import AgenticSession, load_manifest
from redibis.agents.tool_runner import ToolContext
from redibis.config import RedibisConfig
from redibis.profiling.base import ProfileResult
from redibis.quality.rule_set import QualityRuleSet
from redibis.store.contract_store import ContractStore
from redibis.store.storage_backend import LocalBackend

FIXTURE = Path(__file__).parent / "fixtures" / "catalog_telecom_customers.yaml"
SAMPLE = Path(__file__).parent / "data" / "telco_customer_profile.csv"
TABLE = "telecom.customers"


@pytest.fixture
def contract_store(tmp_path) -> ContractStore:
    store = ContractStore(LocalBackend(str(tmp_path / "store")), bucket="active-contracts")
    contract = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    store.upsert(contract, table=TABLE, workflow="test", run_id="t1")
    return store


@pytest.fixture
def lineage(tmp_path) -> LineageStore:
    return LineageStore(tmp_path / "agent_runs")


def _profile_spec() -> PipelineSpec:
    src = PipelineNode(kind="source", label="Src", params={"table": TABLE})
    sample = PipelineNode(kind="sample", label="Sample")
    prof = PipelineNode(kind="profile", label="Profile")
    return PipelineSpec(
        name="langgraph-profile",
        nodes=[src, sample, prof],
        edges=[
            PipelineEdge(source=src.id, target=sample.id),
            PipelineEdge(source=sample.id, target=prof.id),
        ],
    )


@pytest.mark.skipif(not langgraph_available(), reason="langgraph not installed")
def test_langgraph_executor_runs_profile_chain(contract_store, lineage):
    spec = _profile_spec()
    assert validate_spec(spec) == []

    cfg = RedibisConfig()
    cfg.agents.auto_approve_writes = True
    ctx = ToolContext(
        contract_store=contract_store,
        sample_paths={TABLE: str(SAMPLE)},
        sub_store=MagicMock(),
        config=cfg,
    )
    fake_profile = ProfileResult(
        column_profiles=[],
        arabic_columns={},
        triage_signals=[],
        suggested_rules=QualityRuleSet(rules=[]),
        raw={"ge_profiler": MagicMock(expectations=[])},
    )
    executor = LangGraphExecutor(lineage, tool_ctx=ctx, redibis_config=cfg)
    with patch("redibis.scan.base.Scan.profile", return_value=fake_profile):
        with patch("redibis.agents.tool_runner.build_scan_config") as mock_cfg:
            from redibis.services.scan_service import ScanConfig
            mock_cfg.return_value = ScanConfig(table=TABLE, run_pii=False, run_quality=False)
            run, session = executor.execute(spec, TABLE)

    assert run.status == RunStatus.COMPLETED
    assert session.status == RunStatus.COMPLETED
    assert any(
        s.node_kind in ("profile", "profile_scan") and s.status == StepStatus.COMPLETED
        for s in run.steps
    )
    gov = lineage.load_governance_state(run.run_id)
    assert gov is not None
    assert gov.get("transitions")
    restored = load_manifest(lineage.root, session.session_id)
    assert restored.session_id == session.session_id


def test_agentic_session_records_table_run_refs():
    session = AgenticSession(name="batch")
    session.record_table_run(TABLE, "run-abc", session_id="sess-1")
    assert session.table_runs[0].run_id == "run-abc"
    session.record_table_run(TABLE, "run-xyz")
    assert session.table_runs[0].run_id == "run-xyz"


def test_langgraph_compile_rejects_invalid_spec(lineage):
    spec = PipelineSpec(
        name="bad",
        nodes=[PipelineNode(kind="profile", label="P")],
        edges=[],
    )
    executor = LangGraphExecutor(lineage)
    with pytest.raises(ValueError, match="invalid pipeline spec"):
        executor.compile(spec, runtime={})


@pytest.mark.skipif(not langgraph_available(), reason="langgraph not installed")
def test_cooperative_cancel_stops_between_nodes(contract_store, lineage):
    spec = _profile_spec()
    cfg = RedibisConfig()
    cfg.agents.auto_approve_writes = True
    ctx = ToolContext(
        contract_store=contract_store,
        sample_paths={TABLE: str(SAMPLE)},
        sub_store=MagicMock(),
        config=cfg,
    )
    executor = LangGraphExecutor(lineage, tool_ctx=ctx, redibis_config=cfg)
    session = AgenticSession.from_spec(spec)
    token = lineage.cancellation_token(session.session_id)
    token.cancel("user stop")

    run, ended = executor.execute(spec, TABLE, session=session)
    assert run.status == RunStatus.CANCELLED
    assert ended.status == RunStatus.CANCELLED


@pytest.mark.skipif(not langgraph_available(), reason="langgraph not installed")
def test_langgraph_interrupt_pauses_until_resume(contract_store, lineage):
    gate = PipelineNode(kind="gate", label="Steward gate", params={"role": "Steward"})
    spec = PipelineSpec(name="hitl-gate", nodes=[gate], edges=[])
    cfg = RedibisConfig()
    cfg.agents.auto_approve_writes = False
    ctx = ToolContext(contract_store=contract_store, config=cfg, sub_store=MagicMock())
    executor = LangGraphExecutor(lineage, tool_ctx=ctx, redibis_config=cfg)

    with patch("redibis.agents.executor.validate_spec", return_value=[]):
        run, session = executor.execute(spec, TABLE)
    assert run.status == RunStatus.AWAITING_HITL
    assert run.hitl_pending.get("interrupts")
    assert session.status == RunStatus.AWAITING_HITL

    with patch("redibis.agents.executor.validate_spec", return_value=[]):
        run, session = executor.resume(
            spec,
            TABLE,
            agent_run=run,
            agent_session=session,
            resume_value={"approved": True, "approved_by": "steward@test"},
        )
    assert run.status == RunStatus.COMPLETED
    assert not run.hitl_pending
    gate_steps = [s for s in run.steps if s.node_kind in ("gate", "approval_gate")]
    assert gate_steps
