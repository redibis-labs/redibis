"""Auto-document a pipeline or node — read-only growth lever (feature 7, open)."""

from __future__ import annotations

from typing import Any

from redibis.agents.models import PipelineSpec
from redibis.agents.node_registry import get_node, list_nodes
from redibis.agents.prompt_compiler import compile_prompt_plan


def document_pipeline(spec: PipelineSpec) -> dict[str, Any]:
    """Generate what/how/benefits/risks/monitoring prose for a pipeline spec."""
    plan = compile_prompt_plan(spec)
    kinds = [n.kind for n in spec.nodes]
    tools = []
    for kind in kinds:
        try:
            tools.append(get_node(kind).tool)
        except KeyError:
            tools.append(f"plugin:{kind}")

    return {
        "name": spec.name,
        "what": (
            f"Governance pipeline `{spec.name}` orchestrates {len(spec.nodes)} steps "
            f"over configured source tables using deterministic redibis tools."
        ),
        "how": plan.steps,
        "tools": tools,
        "benefits": [
            "Single deterministic core — manual UI, CLI, and agent board share the same tools",
            "Editable prompt-plan export for external LLM agents (v1)",
            "In-app execution with lineage, OTel spans, and cooperative cancellation",
            "Role-routed approval gates before contract writes",
        ],
        "risks": [
            "LLM enrichment is suggest-only — requires human merge unless explicitly configured",
            "Scan steps need representative samples — catalog-only metadata is insufficient for PII",
            "LawfulIntercept classifications escalate to SecOps and are never auto-applied",
            "Generated codegen/Ranger policies are commercial — open core never executes them",
        ],
        "monitoring": [
            "Poll GET /api/agents/runs/{run_id} for batch status and task ledger",
            "GET /api/agents/runs/{run_id}/trace for React Flow DAG audit view",
            "OpenTelemetry spans per step under run_context(run_id)",
            "RAI middleware logs per model call (residency, allow/deny)",
        ],
        "markdown": render_pipeline_markdown(spec, plan),
    }


def render_pipeline_markdown(spec: PipelineSpec, plan: Any = None) -> str:
    if plan is None:
        plan = compile_prompt_plan(spec)
    lines = [
        f"# Pipeline: {spec.name}",
        "",
        "## What",
        spec.goal or f"Governance workflow with {len(spec.nodes)} nodes.",
        "",
        "## How",
        plan.steps,
        "",
        "## Guardrails",
        plan.guardrails,
        "",
        "## Nodes",
    ]
    for node in spec.nodes:
        try:
            ns = get_node(node.kind)
            lines.append(f"- **{ns.label}** (`{ns.tool}`) — {ns.description}")
        except KeyError:
            lines.append(f"- **{node.label or node.kind}** (plugin)")
    lines.extend(["", "## Approval gates", plan.approval_gates])
    return "\n".join(lines) + "\n"


def document_node_kind(kind: str) -> dict[str, Any]:
    """Document a single palette node."""
    spec = get_node(kind)
    return {
        "kind": spec.kind,
        "label": spec.label,
        "tool": spec.tool,
        "description": spec.description,
        "category": spec.category,
        "params": [p.name for p in spec.params],
        "markdown": (
            f"## {spec.label}\n\n"
            f"**Tool:** `{spec.tool}`\n\n"
            f"{spec.description}\n"
        ),
    }


def document_registry() -> dict[str, Any]:
    return {
        "nodes": [document_node_kind(n.kind) for n in list_nodes()],
        "count": len(list_nodes()),
    }
