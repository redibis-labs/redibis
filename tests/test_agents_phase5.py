"""Tests for Phase 5 batch status, review queue, and handoff."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from redibis.agents.handoff import materialize_session_folder
from redibis.agents.models import PipelineNode
from redibis.agents.review_queue import build_review_queue, queue_summary
from redibis.agents.run_models import AgentRun, RunStatus, StepRecord, StepStatus
from redibis.agents.table_status import table_status_rows
from redibis.services.session.state import rehydrate_scan_session


def _sample_run() -> AgentRun:
    node = PipelineNode(id="n1", kind="approval_gate", label="Gate", params={"role": "Steward"})
    run = AgentRun(
        run_id="r1",
        name="test",
        status=RunStatus.COMPLETED,
        tables=["telecom.customers"],
        pipeline={
            "nodes": [node.to_dict()],
            "edges": [],
        },
    )
    run.steps.append(StepRecord(
        step_id="s1",
        node_id="n1",
        node_kind="approval_gate",
        tool="ApprovalGate.await_human",
        table="telecom.customers",
        status=StepStatus.SKIPPED,
        output={"reason": "approval gate — awaits human", "role": "Steward"},
    ))
    run.steps.append(StepRecord(
        step_id="s2",
        node_id="n2",
        node_kind="pii_scan",
        tool="Scan.detect_pii",
        table="telecom.customers",
        status=StepStatus.COMPLETED,
        output={"run_id": "20250101_120000", "columns_detected": 2, "columns_scanned": 5},
    ))
    return run


def test_table_status_rows():
    rows = table_status_rows(_sample_run())
    assert len(rows) == 1
    assert rows[0]["table"] == "telecom.customers"
    assert rows[0]["status"] in ("needs_review", "done", "partial")
    assert rows[0]["run_id"] == "20250101_120000"


def test_run_summary_card_steps_and_metrics():
    from redibis.agents.table_status import run_summary_card

    run = _sample_run()
    card = run_summary_card(run, "telecom.customers")
    assert card["status"] in ("needs_review", "done", "partial")
    assert card["metrics"]["pii_columns"] == 2
    labels = [s["label"] for s in card["steps_done"]]
    assert "PII scanned" in labels
    assert any(s["done"] for s in card["steps_done"] if s["label"] == "PII scanned")


def test_review_queue_includes_approval_and_pii():
    items = build_review_queue(_sample_run())
    kinds = {i.kind for i in items}
    assert "approval" in kinds
    assert "pii" in kinds
    summary = queue_summary(items)
    assert summary["total"] >= 2


def test_materialize_session_and_rehydrate(tmp_path):
    run_dir = tmp_path / "runs" / "rid1"
    run_dir.mkdir(parents=True)
    (run_dir / "pii_contract.yaml").write_text("kind: DataContract\n", encoding="utf-8")
    (run_dir / "quality_contract.yaml").write_text("kind: DataContract\nquality: []\n", encoding="utf-8")
    (run_dir / "quality_results.json").write_text(
        json.dumps([
            {"rule": "expect_column_values_to_not_be_null", "column": "id",
             "success": True, "kwargs": {}},
        ]),
        encoding="utf-8",
    )

    session_dir = tmp_path / "scan_output" / "rid1"
    materialize_session_folder(
        table="telecom.customers",
        run_id="rid1",
        source_run_dir=run_dir,
        session_dir=session_dir,
    )
    session = rehydrate_scan_session(session_dir, tmp_path / "scan_output")
    assert session is not None
    assert session.table_name == "telecom.customers"
    assert session.session_id == "rid1"
    assert "quality_contract" in session.artifacts
    assert session.quality_total == 1
    assert session.runs[0].run_type == "scan_quality"
    assert len(session.runs[0].quality_results) == 1
