"""Tests for agent-run log capture and SSE helpers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from redibis.agents import BatchExecutor, LineageStore, PipelineSpec, ToolContext
from redibis.agents.models import PipelineNode
from redibis.agents.run_log import (
    LIVE_LOG_MAX_LINES,
    append_run_log,
    configure_run_log_root,
    drop_run_log,
    read_full_run_log,
    snapshot_run_log,
)
from redibis.agents.run_models import RunStatus, StepStatus
from redibis.config import RedibisConfig
from redibis.profiling.base import ProfileResult
from redibis.quality.rule_set import QualityRuleSet
from redibis.store.contract_store import ContractStore
from redibis.store.storage_backend import LocalBackend

SAMPLE = Path(__file__).parent / "data" / "telco_customer_profile.csv"
TABLE = "realistic_eshop_customer_account"


@pytest.fixture
def lineage(tmp_path) -> LineageStore:
    return LineageStore(tmp_path / "agent_runs")


def test_append_and_snapshot_run_log():
    rid = "log-test-1"
    try:
        append_run_log(rid, "line one")
        append_run_log(rid, "line two")
        assert snapshot_run_log(rid) == ["line one", "line two"]
    finally:
        drop_run_log(rid)


def test_run_log_tail_and_full_file(tmp_path):
    rid = "log-tail-1"
    root = tmp_path / "agent_runs"
    configure_run_log_root(root)
    try:
        for i in range(LIVE_LOG_MAX_LINES + 25):
            append_run_log(rid, f"line {i}", lineage_root=root)
        tail = snapshot_run_log(rid)
        assert len(tail) == LIVE_LOG_MAX_LINES
        assert tail[0] == f"line {25}"
        full = read_full_run_log(root, rid)
        assert len(full) == LIVE_LOG_MAX_LINES + 25
        assert (root / rid / "run.log").is_file()
    finally:
        drop_run_log(rid)
        configure_run_log_root(None)


def test_contract_classify_uses_scan_partials_without_active_contract(tmp_path, lineage):
    """Composite contract + classify must not require a pre-existing active contract."""
    store = ContractStore(LocalBackend(str(tmp_path / "store")), bucket="active-contracts")
    cfg = RedibisConfig.default()
    cfg.agents.batch_executor = "sequential"
    cfg.classification.enabled = True
    ctx = ToolContext(
        contract_store=store,
        config=cfg,
        sample_paths={TABLE: str(SAMPLE)},
        sub_store=MagicMock(),
        agent_run_id="agent-log-1",
    )
    fake_profile = ProfileResult(
        column_profiles=[],
        arabic_columns={},
        triage_signals=[],
        suggested_rules=QualityRuleSet(rules=[]),
        raw={"ge_profiler": MagicMock(expectations=[])},
    )
    spec = PipelineSpec(
        name="composer-contract",
        nodes=[
            PipelineNode(kind="source_table", label="Src", params={"table": TABLE}),
            PipelineNode(kind="profile_scan", label="Profile"),
            PipelineNode(
                kind="contract",
                label="Contract",
                params={"pii": True, "quality": False, "classify": True, "enrich": False},
            ),
        ],
        edges=[],
    )
    with patch("redibis.scan.base.Scan.profile", return_value=fake_profile):
        with patch("redibis.agents.tool_runner.build_scan_config") as mock_cfg:
            from redibis.services.scan_service import ScanConfig

            mock_cfg.return_value = ScanConfig(table=TABLE, run_pii=True, run_quality=False)
            executor = BatchExecutor(lineage, tool_ctx=ctx)
            run = executor.run(spec, tables=[TABLE])

    assert run.status == RunStatus.COMPLETED, run.error
    contract_steps = [s for s in run.steps if s.node_kind == "contract"]
    assert contract_steps
    assert contract_steps[0].status == StepStatus.COMPLETED
    assert store.get_active(TABLE) is None
    classify_sub = next(
        (sub for sub in (contract_steps[0].output or {}).get("sub_steps") or [] if sub.get("kind") == "classify"),
        None,
    )
    assert classify_sub is not None
    assert not classify_sub.get("skipped")
    assert "tags" in classify_sub
    loaded = lineage.load(run.run_id)
    assert loaded.logs
