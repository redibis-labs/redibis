"""DAG trace — React Flow graph from agent run lineage."""

from __future__ import annotations

from typing import Any

from redibis.agents.models import PipelineSpec
from redibis.agents.run_models import AgentRun, RunStatus, StepStatus


def run_to_react_flow(run: AgentRun) -> dict[str, Any]:
    """Convert an AgentRun into React Flow nodes + edges for audit UI."""
    spec = PipelineSpec.from_dict(run.pipeline) if run.pipeline else PipelineSpec()
    pipeline_nodes = {n.id: n for n in spec.nodes}

    rf_nodes: list[dict[str, Any]] = []
    rf_edges: list[dict[str, Any]] = []

    for edge in spec.edges:
        rf_edges.append({
            "id": edge.id,
            "source": edge.source,
            "target": edge.target,
            "animated": True,
        })

    steps_by_node: dict[str, list] = {}
    for step in run.steps:
        steps_by_node.setdefault(step.node_id, []).append(step)

    for node in spec.nodes:
        steps = steps_by_node.get(node.id, [])
        status = _aggregate_status(steps)
        rf_nodes.append({
            "id": node.id,
            "type": "default",
            "position": dict(node.position),
            "data": {
                "label": node.label or node.kind,
                "kind": node.kind,
                "status": status,
                "tables_done": sum(1 for s in steps if s.status == StepStatus.COMPLETED),
                "tables_total": len(run.tables),
                "step_count": len(steps),
            },
        })

    for node_id, node_steps in steps_by_node.items():
        parent = pipeline_nodes.get(node_id)
        base_pos = dict(parent.position) if parent else {"x": 0, "y": 0}
        for idx, step in enumerate(node_steps):
            sid = f"step-{step.step_id}"
            rf_nodes.append({
                "id": sid,
                "type": "default",
                "position": {
                    "x": base_pos.get("x", 0) + 24,
                    "y": base_pos.get("y", 0) + 72 + (idx * 44),
                },
                "data": {
                    "label": f"{step.table} · {step.status.value}",
                    "kind": "trace_step",
                    "status": step.status.value,
                    "tool": step.tool,
                    "table": step.table,
                    "node_kind": step.node_kind,
                    "error": step.error,
                    "output": step.output,
                },
                "parentNode": node_id,
                "extent": "parent",
            })

    return {
        "run_id": run.run_id,
        "status": run.status.value,
        "nodes": rf_nodes,
        "edges": rf_edges,
        "ledger": run.ledger.reconcile(run.steps),
        "telemetry": list(run.telemetry or []),
    }


def build_audit_dag(run: AgentRun, *, lineage_root: Any = None) -> dict[str, Any]:
    """
    Step-by-step audit DAG — actions, tool calls, RAI hints, human vs auto.

    Sources: OTel spans on the run, step outputs, ledger reconciliation,
    append-only audit events.
    """
    trace = run_to_react_flow(run)
    audit_nodes: list[dict[str, Any]] = []
    seq = 0

    for step in run.steps:
        seq += 1
        out = step.output or {}
        rai = out.get("rai") or out.get("rai_advisories")
        node = {
            "seq": seq,
            "step_id": step.step_id,
            "node_kind": step.node_kind,
            "tool": step.tool,
            "table": step.table,
            "status": step.status.value,
            "span_id": step.span_id,
            "human_required": bool(
                step.status.value == "skipped"
                and "approval" in str(out.get("reason", "")).lower()
            ),
            "rai": rai,
            "error": step.error,
            "enrichment_status": out.get("enrichment_status"),
        }
        audit_nodes.append(node)

    span_by_id = {
        s.get("span_id"): s for s in (run.telemetry or []) if s.get("span_id")
    }
    for node in audit_nodes:
        if node["span_id"] and node["span_id"] in span_by_id:
            node["otel"] = span_by_id[node["span_id"]]

    audit_events: list[dict[str, Any]] = []
    if lineage_root is not None:
        from redibis.agents.run_log import load_audit_events

        audit_events = load_audit_events(lineage_root, run.run_id)

    return {
        "run_id": run.run_id,
        "status": run.status.value,
        "steps": audit_nodes,
        "graph": trace,
        "telemetry_count": len(run.telemetry or []),
        "ledger": run.ledger.reconcile(run.steps),
        "hitl": _hitl_audit_block(run),
        "audit_events": audit_events,
    }


def _hitl_audit_block(run: AgentRun) -> dict[str, Any]:
    if run.status != RunStatus.AWAITING_HITL and not run.hitl_pending:
        return {}
    return {
        "status": run.status.value,
        "pending": dict(run.hitl_pending or {}),
        "batch_meta": dict(run.batch_meta or {}),
        "session_id": run.session_id,
    }


def _aggregate_status(steps: list) -> str:
    if not steps:
        return "pending"
    if any(s.status == StepStatus.FAILED for s in steps):
        return "failed"
    if any(s.status == StepStatus.RUNNING for s in steps):
        return "running"
    if all(s.status in (StepStatus.COMPLETED, StepStatus.SKIPPED) for s in steps):
        return "completed"
    return "partial"
