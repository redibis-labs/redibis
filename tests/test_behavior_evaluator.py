"""Tests for behavior evaluator and reducer."""

from __future__ import annotations

import pytest

from redibis.behavior.compiler import compile_policy, compile_rules_for_evaluation
from redibis.behavior.evaluator import PolicyEvaluator, sort_rules
from redibis.behavior.models import (
    Authority,
    BehaviorCapabilities,
    BehaviorContext,
    HookStage,
    RuleTraceStatus,
    TruthValue,
)
from redibis.behavior.reducer import PatchReducer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ctx(facts: dict, stage=HookStage.POST_VERDICT, table="test.table", column="col"):
    return BehaviorContext(
        run_id="r1",
        table=table,
        column=column,
        engine="pii",
        stage=stage,
        facts=facts,
        capabilities=BehaviorCapabilities(
            allowed_patch_operations=frozenset({
                "verdict.entity", "verdict.detected", "verdict.confidence",
                "review.require", "review.add_reason",
                "threshold.presidio_min", "threshold.gliner_min",
            }),
        ),
        registry_manifest_sha256="",
    )


def _compile(doc):
    policy = compile_policy(doc)
    return compile_rules_for_evaluation(policy)


BASE_DOC = {
    "apiVersion": "redibis.io/behavior-policy/v1",
    "kind": "BehaviorPolicy",
    "metadata": {"id": "test", "version": "1.0.0"},
    "appliesTo": {"engine": "pii", "stage": "post_verdict"},
    "rules": [],
}


def _rule(id, when, effects, reason="test reason", priority=100, terminal=False):
    return {
        "id": id,
        "when": when,
        "effects": effects,
        "reason": reason,
        "priority": priority,
        "terminal": terminal,
    }


# ---------------------------------------------------------------------------
# Basic match / no-match
# ---------------------------------------------------------------------------

def test_rule_matches():
    doc = dict(BASE_DOC, rules=[
        _rule(
            "r1",
            {"fact": "verdict.entity", "op": "eq", "value": "PHONE_NUMBER"},
            [{"effect": "core.verdict.set_entity", "params": {"entity": "NETWORK_ID"}}],
        )
    ])
    rules = _compile(doc)
    ctx = _make_ctx({"verdict.entity": "PHONE_NUMBER", "verdict.detected": True})
    evaluator = PolicyEvaluator()
    traces, patch = evaluator.evaluate(rules, ctx, stage=HookStage.POST_VERDICT)
    matched = [t for t in traces if t.status == RuleTraceStatus.MATCHED]
    assert len(matched) == 1
    assert matched[0].rule_id == "r1"
    assert len(patch.operations) > 0


def test_rule_no_match():
    doc = dict(BASE_DOC, rules=[
        _rule(
            "r1",
            {"fact": "verdict.entity", "op": "eq", "value": "PHONE_NUMBER"},
            [{"effect": "core.verdict.set_entity", "params": {"entity": "NETWORK_ID"}}],
        )
    ])
    rules = _compile(doc)
    ctx = _make_ctx({"verdict.entity": "EMAIL_ADDRESS"})
    evaluator = PolicyEvaluator()
    traces, patch = evaluator.evaluate(rules, ctx, stage=HookStage.POST_VERDICT)
    matched = [t for t in traces if t.status == RuleTraceStatus.MATCHED]
    assert not matched
    assert not patch.operations


# ---------------------------------------------------------------------------
# Unknown fact → no-match
# ---------------------------------------------------------------------------

def test_unknown_fact_no_match():
    doc = dict(BASE_DOC, rules=[
        _rule(
            "r1",
            {"fact": "missing.fact", "op": "eq", "value": "X"},
            [{"effect": "core.review.require", "params": {}}],
        )
    ])
    rules = _compile(doc)
    ctx = _make_ctx({})
    evaluator = PolicyEvaluator()
    traces, patch = evaluator.evaluate(rules, ctx, stage=HookStage.POST_VERDICT)
    unknown_traces = [t for t in traces if t.status == RuleTraceStatus.UNKNOWN_NO_MATCH]
    assert unknown_traces


# ---------------------------------------------------------------------------
# Priority ordering
# ---------------------------------------------------------------------------

def test_priority_ordering():
    """Higher priority rule must be evaluated first."""
    doc = dict(BASE_DOC, rules=[
        _rule("r-low", {"fact": "verdict.entity", "op": "eq", "value": "PHONE_NUMBER"},
              [{"effect": "core.verdict.set_entity", "params": {"entity": "LOW"}}],
              priority=100),
        _rule("r-high", {"fact": "verdict.entity", "op": "eq", "value": "PHONE_NUMBER"},
              [{"effect": "core.verdict.set_entity", "params": {"entity": "HIGH"}}],
              priority=200),
    ])
    rules = _compile(doc)
    sorted_r = sort_rules(rules)
    assert sorted_r[0].id == "r-high"
    assert sorted_r[1].id == "r-low"


# ---------------------------------------------------------------------------
# Terminal stop
# ---------------------------------------------------------------------------

def test_terminal_stops_lower_rules():
    doc = dict(BASE_DOC, rules=[
        _rule("r-terminal", {"fact": "verdict.entity", "op": "eq", "value": "PHONE_NUMBER"},
              [{"effect": "core.verdict.set_entity", "params": {"entity": "NETWORK_ID"}}],
              priority=200, terminal=True),
        _rule("r-lower", {"fact": "verdict.entity", "op": "eq", "value": "PHONE_NUMBER"},
              [{"effect": "core.review.require", "params": {}}],
              priority=100),
    ])
    rules = _compile(doc)
    ctx = _make_ctx({"verdict.entity": "PHONE_NUMBER"})
    evaluator = PolicyEvaluator()
    traces, patch = evaluator.evaluate(rules, ctx, stage=HookStage.POST_VERDICT)
    terminated = [t for t in traces if t.status == RuleTraceStatus.TERMINATED]
    assert terminated
    assert terminated[0].rule_id == "r-lower"


# ---------------------------------------------------------------------------
# Stage filtering
# ---------------------------------------------------------------------------

def test_rule_out_of_scope_for_stage():
    doc = dict(BASE_DOC, rules=[
        _rule("r1", {"fact": "verdict.entity", "op": "eq", "value": "X"},
              [{"effect": "core.verdict.set_entity", "params": {"entity": "Y"}}])
    ])
    rules = _compile(doc)
    ctx = _make_ctx({"verdict.entity": "X"}, stage=HookStage.PRE_VERDICT)
    evaluator = PolicyEvaluator()
    traces, patch = evaluator.evaluate(rules, ctx, stage=HookStage.PRE_VERDICT)
    # The compiled rule was for POST_VERDICT, so it's out-of-scope for PRE_VERDICT
    oos = [t for t in traces if t.status == RuleTraceStatus.OUT_OF_SCOPE]
    assert oos


# ---------------------------------------------------------------------------
# Reducer tests
# ---------------------------------------------------------------------------

def test_reducer_allows_permitted_operation():
    from redibis.behavior.models import BehaviorPatch, PatchOperation
    caps = BehaviorCapabilities(
        allowed_patch_operations=frozenset({"verdict.entity"}),
    )
    reducer = PatchReducer(caps)
    op = PatchOperation(
        op="set", path="verdict.entity", value="NETWORK_ID",
        authority=Authority.APPROVED_POLICY, reason="test",
    )
    patch = BehaviorPatch(operations=(op,))
    applied, _, blocked = reducer.reduce(patch)
    assert len(applied.operations) == 1
    assert not blocked


def test_reducer_blocks_unsupported_operation():
    from redibis.behavior.models import BehaviorPatch, PatchOperation
    caps = BehaviorCapabilities(
        allowed_patch_operations=frozenset({"verdict.entity"}),
    )
    reducer = PatchReducer(caps)
    op = PatchOperation(
        op="set", path="something.not_allowed", value="X",
        authority=Authority.APPROVED_POLICY, reason="test",
    )
    patch = BehaviorPatch(operations=(op,))
    applied, _, blocked = reducer.reduce(patch)
    assert not applied.operations
    assert blocked


def test_reducer_human_decision_blocks_policy():
    from redibis.behavior.models import BehaviorPatch, PatchOperation
    caps = BehaviorCapabilities(
        allowed_patch_operations=frozenset({"verdict.entity"}),
    )
    human_decisions = {"verdict.entity": Authority.HUMAN_DECISION}
    reducer = PatchReducer(caps, human_decisions=human_decisions)
    op = PatchOperation(
        op="set", path="verdict.entity", value="NETWORK_ID",
        authority=Authority.APPROVED_POLICY, reason="policy correction",
    )
    patch = BehaviorPatch(operations=(op,))
    applied, _, blocked = reducer.reduce(patch)
    assert not applied.operations
    assert blocked
    assert "HUMAN_DECISION" in blocked[0].blocked_by


def test_reducer_higher_authority_wins_conflict():
    from redibis.behavior.models import BehaviorPatch, PatchOperation
    caps = BehaviorCapabilities(
        allowed_patch_operations=frozenset({"verdict.entity"}),
    )
    reducer = PatchReducer(caps)
    op_low = PatchOperation(
        op="set", path="verdict.entity", value="ENTITY_A",
        authority=Authority.ENGINE_NATIVE, reason="engine",
    )
    op_high = PatchOperation(
        op="set", path="verdict.entity", value="ENTITY_B",
        authority=Authority.APPROVED_POLICY, reason="policy",
    )
    patch = BehaviorPatch(operations=(op_low, op_high))
    applied, _, blocked = reducer.reduce(patch)
    # High authority wins
    assert any(op.value == "ENTITY_B" for op in applied.operations)


def test_reducer_conflict_same_authority_routes_to_review():
    from redibis.behavior.models import BehaviorPatch, PatchOperation
    caps = BehaviorCapabilities(
        allowed_patch_operations=frozenset({"verdict.entity"}),
    )
    reducer = PatchReducer(caps)
    op_a = PatchOperation(
        op="set", path="verdict.entity", value="ENTITY_A",
        authority=Authority.APPROVED_POLICY, reason="rule a",
        rule_id="r1",
    )
    op_b = PatchOperation(
        op="set", path="verdict.entity", value="ENTITY_B",
        authority=Authority.APPROVED_POLICY, reason="rule b",
        rule_id="r2",
    )
    patch = BehaviorPatch(operations=(op_a, op_b))
    applied, _, blocked = reducer.reduce(patch)
    assert blocked   # at least one is blocked
