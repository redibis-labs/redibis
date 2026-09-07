"""Prompt-plan compiler — board graph → editable sectioned plan."""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

from redibis.agents.guardrails import render_guardrails
from redibis.agents.models import PipelineSpec, PromptPlan, PromptPlanSection
from redibis.agents.node_registry import NODE_REGISTRY, get_node


def topological_order(spec: PipelineSpec) -> list[str]:
    """Return node ids in dependency order (sources first)."""
    adj: dict[str, list[str]] = defaultdict(list)
    indegree: dict[str, int] = {n.id: 0 for n in spec.nodes}
    for edge in spec.edges:
        adj[edge.source].append(edge.target)
        indegree[edge.target] = indegree.get(edge.target, 0) + 1

    queue: deque[str] = deque(nid for nid, deg in indegree.items() if deg == 0)
    order: list[str] = []
    while queue:
        nid = queue.popleft()
        order.append(nid)
        for nxt in adj.get(nid, []):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)

    if len(order) != len(spec.nodes):
        remaining = [n.id for n in spec.nodes if n.id not in order]
        order.extend(remaining)
    return order


def compile_prompt_plan(spec: PipelineSpec) -> PromptPlan:
    """Compile a PipelineSpec into an export-only editable prompt-plan."""
    node_by_id = {n.id: n for n in spec.nodes}
    order = topological_order(spec)

    source_lines: list[str] = []
    if spec.source:
        for key, val in spec.source.items():
            source_lines.append(f"- {key}: {val}")
    else:
        for nid in order:
            node = node_by_id.get(nid)
            if node and node.kind == "source_table":
                spec_def = get_node(node.kind)
                source_lines.append(f"- {spec_def.render_prompt_fragment(node.params)}")

    step_lines: list[str] = []
    step_node_ids: list[str] = []
    for i, nid in enumerate(order, 1):
        node = node_by_id.get(nid)
        if not node or node.kind == "source_table":
            continue
        spec_def = NODE_REGISTRY.get(node.kind)
        if not spec_def:
            step_lines.append(f"{i}. [{node.label or node.kind}] (unknown tool)")
            continue
        fragment = spec_def.render_prompt_fragment(node.params)
        step_lines.append(f"{i}. **{spec_def.label}** (`{spec_def.tool}`): {fragment}")
        step_node_ids.append(nid)

    approval_lines: list[str] = []
    for node in spec.nodes:
        if node.kind == "approval_gate":
            role = node.params.get("role", "Steward")
            approval_lines.append(f"- Await {role} approval before downstream writes.")

    if not approval_lines:
        approval_lines.append("- Steward must approve all LLM-derived suggestions before contract write.")

    plan = PromptPlan(
        goal=spec.goal or f"Governance pipeline: {spec.name}",
        source="\n".join(source_lines) if source_lines else "(configure a Source Table node)",
        steps="\n".join(step_lines) if step_lines else "(add scan/governance nodes)",
        guardrails=render_guardrails(),
        output_format=(
            "Emit an ODCS v3 contract partial per table with:\n"
            "- column privacy blocks (classification_engine + masking_policy intent)\n"
            "- quality rules (human-approved only)\n"
            "- slaProperties.retention when policy engine derives it\n"
            "- classification tags ready for atomic Atlas push"
        ),
        approval_gates="\n".join(approval_lines),
        pipeline=spec,
    )

    plan.sections = [
        PromptPlanSection("goal", "Goal", plan.goal, locked=False),
        PromptPlanSection("source", "Source", plan.source, locked=False,
                          source_nodes=[n.id for n in spec.nodes if n.kind == "source_table"]),
        PromptPlanSection("steps", "Steps", plan.steps, locked=False, source_nodes=step_node_ids),
        PromptPlanSection("guardrails", "Guardrails", plan.guardrails, locked=True),
        PromptPlanSection("output_format", "Output format", plan.output_format, locked=False),
        PromptPlanSection("approval_gates", "Approval gates", plan.approval_gates, locked=False),
    ]
    return plan


def merge_manual_edits(
    plan: PromptPlan,
    edits: dict[str, str],
) -> PromptPlan:
    """Apply manual prompt edits to unlocked sections (v1 export tuning)."""
    field_map = {
        "goal": "goal",
        "source": "source",
        "steps": "steps",
        "output_format": "output_format",
        "approval_gates": "approval_gates",
    }
    for key, attr in field_map.items():
        if key in edits:
            setattr(plan, attr, edits[key])
    for section in plan.sections:
        if not section.locked and section.key in edits:
            section.content = edits[section.key]
    return plan


def diff_prompt_sections(
    before: PromptPlan,
    after: PromptPlan,
) -> dict[str, Any]:
    """Return changed sections for graph-regeneration diff UX."""
    changes: dict[str, dict[str, str]] = {}
    before_map = {s.key: s.content for s in before.sections}
    after_map = {s.key: s.content for s in after.sections}
    for key in set(before_map) | set(after_map):
        if before_map.get(key) != after_map.get(key):
            changes[key] = {"before": before_map.get(key, ""), "after": after_map.get(key, "")}
    return changes
