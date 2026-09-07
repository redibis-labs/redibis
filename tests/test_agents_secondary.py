"""AGENTS-005 — lineage, session manifest, HITL surfacing in queue/status/audit."""

from __future__ import annotations

from pathlib import Path

from redibis.agents.dag_trace import build_audit_dag
from redibis.agents.lineage_store import LineageStore
from redibis.agents.review_queue import build_review_queue
from redibis.agents.run_models import AgentRun, RunStatus
from redibis.agents.session import AgenticSession, load_manifest, save_manifest
from redibis.agents.table_status import table_status_rows


def test_lineage_round_trip_hitl_and_batch_meta(tmp_path):
    store = LineageStore(tmp_path / "runs")
    run = AgentRun(
        name="batch-hitl",
        status=RunStatus.AWAITING_HITL,
        tables=["telecom.customers", "telecom.subscribers"],
        session_id="sess-abc",
        hitl_pending={
            "interrupts": [{
                "id": "intr-1",
                "value": {
                    "node_kind": "gate",
                    "table": "telecom.customers",
                    "role": "Steward",
                },
            }],
        },
        batch_meta={
            "executor": "langgraph",
            "paused_table": "telecom.customers",
            "session_id": "sess-abc",
        },
    )
    store.save(run)
    loaded = store.load(run.run_id)
    assert loaded.status == RunStatus.AWAITING_HITL
    assert loaded.batch_meta["paused_table"] == "telecom.customers"
    assert loaded.hitl_pending["interrupts"][0]["id"] == "intr-1"


def test_session_manifest_round_trip_langgraph_thread(tmp_path):
    root = tmp_path / "sessions"
    session = AgenticSession(
        session_id="sess-xyz",
        name="hitl-run",
        status=RunStatus.AWAITING_HITL,
        langgraph_thread_id="batch-run-id:telecom.customers",
    )
    session.record_table_run("telecom.customers", "run-folder-1")
    save_manifest(root, session)
    restored = load_manifest(root, "sess-xyz")
    assert restored.langgraph_thread_id == "batch-run-id:telecom.customers"
    assert restored.table_runs[0].run_id == "run-folder-1"
    assert restored.status == RunStatus.AWAITING_HITL


def test_review_queue_surfaces_langgraph_interrupt():
    run = AgentRun(
        status=RunStatus.AWAITING_HITL,
        session_id="sess-1",
        hitl_pending={
            "interrupts": [{
                "id": "intr-9",
                "value": {"node_kind": "gate", "table": "db.t", "role": "Steward"},
            }],
        },
    )
    items = build_review_queue(run)
    assert len(items) == 1
    assert items[0].kind == "approval"
    assert items[0].table == "db.t"
    assert items[0].provenance.get("source") == "langgraph_interrupt"


def test_table_status_marks_paused_table_awaiting_hitl():
    run = AgentRun(
        status=RunStatus.AWAITING_HITL,
        tables=["db.a", "db.b"],
        batch_meta={"paused_table": "db.b"},
        hitl_pending={"interrupts": [{"id": "x", "value": {"table": "db.b", "node_kind": "gate"}}]},
    )
    rows = table_status_rows(run)
    by_table = {r["table"]: r["status"] for r in rows}
    assert by_table["db.b"] == "awaiting_hitl"
    assert by_table["db.a"] == "queued"


def test_audit_dag_includes_hitl_block():
    run = AgentRun(
        run_id="r-hitl",
        status=RunStatus.AWAITING_HITL,
        session_id="sess-2",
        batch_meta={"executor": "langgraph", "paused_table": "db.t"},
        hitl_pending={"interrupts": [{"id": "i1", "value": {"table": "db.t"}}]},
    )
    audit = build_audit_dag(run)
    assert audit["hitl"]["session_id"] == "sess-2"
    assert audit["hitl"]["batch_meta"]["paused_table"] == "db.t"
    assert audit["hitl"]["pending"]["interrupts"]
