"""Tests for audit DAG and deep profile catalogue (Phases 6–7)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from redibis.agents.dag_trace import build_audit_dag
from redibis.agents.deep_profile import get_capability, list_capabilities, run_deep_profile
from redibis.agents.run_models import AgentRun, RunStatus, StepRecord, StepStatus


def test_build_audit_dag_includes_steps_and_telemetry():
    run = AgentRun(
        run_id="a1",
        status=RunStatus.COMPLETED,
        telemetry=[{"span_id": "sp1", "name": "agent.step", "attributes": {}}],
    )
    run.steps.append(StepRecord(
        step_id="s1",
        node_id="n1",
        node_kind="profile",
        tool="Scan.profile",
        table="db.t",
        status=StepStatus.COMPLETED,
        span_id="sp1",
        output={"columns": 3},
    ))
    audit = build_audit_dag(run)
    assert audit["run_id"] == "a1"
    assert len(audit["steps"]) == 1
    assert audit["steps"][0]["otel"]["name"] == "agent.step"


def test_profiling_capabilities_load():
    caps = list_capabilities()
    ids = {c.id for c in caps}
    assert "ge.value_set_expectations" in ids
    assert "native.relationships" in ids
    assert "ydata.correlations" in ids
    from redibis.profiling.ydata_report import ydata_available

    if ydata_available():
        assert "ydata.profile_report" in ids
    else:
        assert "ydata.profile_report" not in ids


def test_ydata_profile_report_gated_without_extra(tmp_path):
    from redibis.profiling.ydata_report import ydata_available

    if ydata_available():
        pytest.skip("ydata installed — gating test applies only without the extra")
    result = run_deep_profile(
        "db.t",
        "run1",
        ["ydata.profile_report"],
        run_dir=tmp_path,
        df=pd.DataFrame({"a": [1, 2]}),
    )
    assert result["errors"]
    assert any("ydata.profile_report" in e for e in result["errors"])


def test_run_deep_profile_writes_artifacts(tmp_path):
    df = pd.DataFrame({"a": [1, 2, 3], "b": [4, 5, 6]})
    result = run_deep_profile(
        "db.t",
        "run1",
        ["ge.value_set_expectations", "ydata.correlations"],
        {"ydata.correlations": {"methods": ["pearson"]}},
        run_dir=tmp_path,
        df=df,
    )
    assert not result["errors"]
    assert len(result["reports"]) == 2
    corr_report = next(r for r in result["reports"] if r["capability_id"] == "ydata.correlations")
    assert corr_report["summary"]["source"] == "pandas"
    assert Path(result["manifest"]).is_file()


def test_unknown_capability_rejected(tmp_path):
    result = run_deep_profile("db.t", "r", ["not.real"], run_dir=tmp_path, df=pd.DataFrame({"x": [1]}))
    assert result["errors"]
