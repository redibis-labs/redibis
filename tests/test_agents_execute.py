"""Tests for Phase 5 in-app pipeline execution."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from redibis.agents import BatchExecutor, LineageStore, PipelineSpec, ToolContext
from redibis.agents.models import PipelineNode
from redibis.agents.pipeline_executor import PipelineExecutor
from redibis.agents.run_models import RunStatus, StepStatus
from redibis.agents.sample_loader import resolve_sample_path
from redibis.agents.tool_runner import run_node_step
from redibis.config import RedibisConfig
from redibis.profiling.base import ProfileResult
from redibis.quality.rule_set import QualityRuleSet
from redibis.store.contract_store import ContractStore
from redibis.store.storage_backend import LocalBackend

FIXTURE = Path(__file__).parent / "fixtures" / "catalog_telecom_customers.yaml"
SAMPLE = Path(__file__).parent / "data" / "telco_customer_profile.csv"
TABLE = "telecom.customers"


def _sequential_ctx(**kwargs) -> ToolContext:
    cfg = RedibisConfig.default()
    cfg.agents.batch_executor = "sequential"
    return ToolContext(config=cfg, **kwargs)


@pytest.fixture
def contract_store(tmp_path) -> ContractStore:
    store = ContractStore(LocalBackend(str(tmp_path / "store")), bucket="active-contracts")
    contract = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    store.upsert(contract, table=TABLE, workflow="test", run_id="t1")
    return store


@pytest.fixture
def lineage(tmp_path) -> LineageStore:
    return LineageStore(tmp_path / "agent_runs")


def test_resolve_sample_path_explicit():
    path = resolve_sample_path(
        TABLE,
        sample_paths={TABLE: str(SAMPLE)},
    )
    assert path == SAMPLE


def test_profile_scan_with_mock(contract_store, lineage):
    spec = PipelineSpec(
        name="profile-only",
        nodes=[
            PipelineNode(kind="source_table", label="Src", params={"table": TABLE}),
            PipelineNode(kind="profile_scan", label="Profile"),
        ],
        edges=[],
    )
    fake_profile = ProfileResult(
        column_profiles=[],
        arabic_columns={},
        triage_signals=[],
        suggested_rules=QualityRuleSet(rules=[]),
        raw={"ge_profiler": MagicMock(expectations=[])},
    )
    ctx = _sequential_ctx(
        contract_store=contract_store,
        sample_paths={TABLE: str(SAMPLE)},
        sub_store=MagicMock(),
    )
    with patch("redibis.scan.base.Scan.profile", return_value=fake_profile):
        with patch("redibis.agents.tool_runner.build_scan_config") as mock_cfg:
            from redibis.services.scan_service import ScanConfig
            mock_cfg.return_value = ScanConfig(table=TABLE, run_pii=False, run_quality=False)
            executor = BatchExecutor(lineage, tool_ctx=ctx)
            run = executor.run(spec, tables=[TABLE])
    profile_steps = [s for s in run.steps if s.node_kind == "profile_scan"]
    assert profile_steps
    assert profile_steps[0].status == StepStatus.COMPLETED


def test_pipeline_executor_single_table(contract_store, lineage):
    spec = PipelineSpec(
        name="classify-only",
        nodes=[
            PipelineNode(kind="classify", label="Cls", params={"policy_pack": "telecom"}),
        ],
        edges=[],
    )
    ctx = ToolContext(contract_store=contract_store, sub_store=MagicMock())
    executor = PipelineExecutor(lineage, tool_ctx=ctx)
    run = executor.execute(spec, TABLE, use_langgraph=False)
    assert run.status == RunStatus.COMPLETED
    assert any(s.node_kind == "classify" and s.status == StepStatus.COMPLETED for s in run.steps)


def test_pipeline_executor_prefers_langgraph_when_installed(contract_store, lineage):
    from redibis.agents.pipeline_executor import langgraph_available

    if not langgraph_available():
        pytest.skip("langgraph not installed")

    spec = PipelineSpec(
        name="classify-only-lg",
        nodes=[
            PipelineNode(kind="classify", label="Cls", params={"policy_pack": "telecom"}),
        ],
        edges=[],
    )
    ctx = ToolContext(contract_store=contract_store, sub_store=MagicMock())
    executor = PipelineExecutor(lineage, tool_ctx=ctx)
    with patch("redibis.agents.executor.validate_spec", return_value=[]):
        run = executor.execute(spec, TABLE)
    assert run.session_id


def test_contract_write_requires_approval(contract_store):
    node = PipelineNode(
        kind="contract_write",
        label="Write",
        params={"automerge": "none"},
    )
    ctx = ToolContext(contract_store=contract_store, sub_store=MagicMock())
    step = run_node_step(node, table=TABLE, ctx=ctx)
    assert step.status == StepStatus.SKIPPED
    assert "approval" in step.output.get("reason", "")
