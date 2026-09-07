"""Tests for agentic pipeline board (export-only v1)."""

from __future__ import annotations

from redibis.agents import (
    PipelineSpec,
    compile_prompt_plan,
    diff_prompt_sections,
    list_nodes,
    merge_manual_edits,
)
from redibis.agents.guardrails import GUARDRAIL_CLAUSES, render_guardrails
from redibis.agents.models import PipelineEdge, PipelineNode
from redibis.cli.agents_cmd import _example_pipeline


def test_node_registry_lists_core_tools():
    kinds = {n.kind for n in list_nodes()}
    assert "source_table" in kinds
    assert "pii_scan" in kinds
    assert "classify" in kinds
    assert "contract_write" in kinds


def test_compile_example_pipeline():
    spec = _example_pipeline()
    plan = compile_prompt_plan(spec)
    assert "Goal" in plan.to_text() or plan.goal
    assert "ContractStore.upsert" in plan.guardrails
    assert "PII Scan" in plan.steps or "pii" in plan.steps.lower()
    assert len(plan.sections) >= 5


def test_guardrails_locked():
    text = render_guardrails()
    assert "LawfulIntercept" in text
    assert len(GUARDRAIL_CLAUSES) >= 5


def test_manual_edits_merge():
    spec = _example_pipeline()
    before = compile_prompt_plan(spec)
    after = merge_manual_edits(compile_prompt_plan(spec), {"goal": "Custom governance goal"})
    assert after.goal == "Custom governance goal"
    diff = diff_prompt_sections(before, after)
    assert "goal" in diff


def test_pipeline_roundtrip():
    spec = _example_pipeline()
    restored = PipelineSpec.from_dict(spec.to_dict())
    assert restored.name == spec.name
    assert len(restored.nodes) == len(spec.nodes)
    assert len(restored.edges) == len(spec.edges)


def test_topological_steps_order():
    a = PipelineNode(kind="source_table", label="Source")
    b = PipelineNode(kind="profile_scan", label="Profile")
    c = PipelineNode(kind="pii_scan", label="PII")
    spec = PipelineSpec(
        name="test",
        nodes=[a, b, c],
        edges=[
            PipelineEdge(source=a.id, target=b.id),
            PipelineEdge(source=b.id, target=c.id),
        ],
    )
    plan = compile_prompt_plan(spec)
    assert plan.steps.index("Profile") < plan.steps.index("PII")
