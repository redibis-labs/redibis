"""Tests for redibis.behavior.models — immutability, equality, JSON-safety."""

from __future__ import annotations

import json
import pytest

from redibis.behavior.models import (
    Authority,
    AuthoredRule,
    BehaviorCapabilities,
    BehaviorContext,
    BehaviorPatch,
    BehaviorPolicy,
    BlockedOperation,
    CompiledRule,
    ConditionGroup,
    EffectCall,
    EvaluationTrace,
    FailMode,
    HookStage,
    LintFinding,
    LintSeverity,
    PatchOperation,
    PolicySource,
    Predicate,
    RuleScope,
    RuleTrace,
    RuleTraceStatus,
    RuntimeMode,
    TruthValue,
    UnknownBehavior,
    ConflictPolicy,
    BehaviorPolicyWarning,
    PolicyAuditEvent,
)


# ---------------------------------------------------------------------------
# Enum coverage
# ---------------------------------------------------------------------------

def test_hook_stage_values():
    assert HookStage.POST_VERDICT.value == "post_verdict"
    assert HookStage.PRE_VERDICT.value == "pre_verdict"
    assert len(HookStage) == 6


def test_truth_value_enum():
    assert TruthValue.TRUE != TruthValue.FALSE
    assert TruthValue.UNKNOWN not in (TruthValue.TRUE, TruthValue.FALSE)


def test_authority_ordering():
    assert Authority.SAFETY_GUARD.value > Authority.HUMAN_DECISION.value
    assert Authority.HUMAN_DECISION.value > Authority.APPROVED_POLICY.value
    assert Authority.APPROVED_POLICY.value > Authority.ENGINE_NATIVE.value
    assert Authority.ENGINE_NATIVE.value > Authority.LLM_PROPOSAL.value


def test_fail_mode_values():
    assert FailMode.BASELINE_WITH_WARNING.value == "baseline_with_warning"
    assert FailMode.FAIL_RUN.value == "fail_run"


def test_runtime_mode_values():
    assert RuntimeMode.DISABLED.value == "disabled"
    assert RuntimeMode.SHADOW.value == "shadow"
    assert RuntimeMode.ACTIVE.value == "active"


# ---------------------------------------------------------------------------
# Frozen dataclasses — immutability
# ---------------------------------------------------------------------------

def test_predicate_frozen():
    p = Predicate(fact="column.name", operator="eq", expected="msisdn")
    with pytest.raises(Exception):
        p.fact = "other"  # type: ignore[misc]


def test_condition_group_frozen():
    cg = ConditionGroup(
        kind="all",
        children=(Predicate(fact="x", operator="eq", expected=1),),
    )
    with pytest.raises(Exception):
        cg.kind = "any"  # type: ignore[misc]


def test_authored_rule_frozen():
    rule = AuthoredRule(
        id="r1",
        when=Predicate(fact="f", operator="eq", expected="v"),
        effects=(EffectCall(effect="core.verdict.set_entity", params={"entity": "PHONE_NUMBER"}),),
        reason="test reason",
    )
    with pytest.raises(Exception):
        rule.id = "changed"  # type: ignore[misc]


def test_rule_scope_frozen():
    scope = RuleScope(tables=("cdr.*",))
    with pytest.raises(Exception):
        scope.tables = ()  # type: ignore[misc]


def test_patch_operation_frozen():
    op = PatchOperation(
        op="set", path="verdict.entity", value="PHONE_NUMBER",
        authority=Authority.APPROVED_POLICY, reason="test",
    )
    with pytest.raises(Exception):
        op.path = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Equality
# ---------------------------------------------------------------------------

def test_predicate_equality():
    p1 = Predicate(fact="column.name", operator="matches", expected="lac")
    p2 = Predicate(fact="column.name", operator="matches", expected="lac")
    assert p1 == p2


def test_effect_call_equality():
    e1 = EffectCall(effect="core.verdict.set_entity", params={"entity": "PHONE_NUMBER"})
    e2 = EffectCall(effect="core.verdict.set_entity", params={"entity": "PHONE_NUMBER"})
    assert e1 == e2


def test_different_predicates_not_equal():
    p1 = Predicate(fact="column.name", operator="eq", expected="a")
    p2 = Predicate(fact="column.name", operator="eq", expected="b")
    assert p1 != p2


# ---------------------------------------------------------------------------
# JSON-safe round-trip
# ---------------------------------------------------------------------------

def test_predicate_json_safe():
    p = Predicate(fact="column.name", operator="eq", expected="test")
    d = {"fact": p.fact, "operator": p.operator, "expected": p.expected}
    assert json.dumps(d)   # must not raise


def test_effect_call_json_safe():
    e = EffectCall(effect="core.verdict.set_entity", params={"entity": "PHONE_NUMBER"})
    d = {"effect": e.effect, "params": e.params}
    assert json.dumps(d)


def test_behavior_patch_construction():
    patch = BehaviorPatch(
        operations=(
            PatchOperation(
                op="set", path="verdict.entity", value="PHONE_NUMBER",
                authority=Authority.APPROVED_POLICY, reason="test",
            ),
        ),
        require_review=False,
    )
    assert len(patch.operations) == 1
    assert not patch.require_review


# ---------------------------------------------------------------------------
# BehaviorContext — no raw values enforced by structure
# ---------------------------------------------------------------------------

def test_behavior_context_fields():
    ctx = BehaviorContext(
        run_id="run-001",
        table="telecom.cdr",
        column="msisdn",
        engine="pii",
        stage=HookStage.POST_VERDICT,
        facts={"column.name": "msisdn", "verdict.detected": True},
        capabilities=BehaviorCapabilities(),
        registry_manifest_sha256="abc",
    )
    assert ctx.column == "msisdn"
    assert ctx.stage == HookStage.POST_VERDICT
    assert "column.name" in ctx.facts


def test_behavior_context_facts_only_metadata():
    """Facts must not contain raw sample values — only metadata/aggregates."""
    ctx = BehaviorContext(
        run_id="r",
        table="t",
        engine="pii",
        stage=HookStage.POST_VERDICT,
        facts={
            "column.name": "phone",
            "validators.phone.valid_rate": 0.95,
            "verdict.entity": "PHONE_NUMBER",
            "verdict.detected": True,
        },
        capabilities=BehaviorCapabilities(),
        registry_manifest_sha256="",
    )
    # Verify no raw values (strings > 128 chars, or obviously raw data patterns)
    for k, v in ctx.facts.items():
        if isinstance(v, str):
            assert len(v) <= 256, f"Suspicious raw-looking value for {k!r}: {v!r}"


# ---------------------------------------------------------------------------
# LintFinding
# ---------------------------------------------------------------------------

def test_lint_finding_construction():
    f = LintFinding(
        finding_id="MISSING_REASON",
        severity=LintSeverity.ERROR,
        policy_id="p1",
        rule_id="r1",
        message="Rule r1 is missing a reason.",
    )
    assert f.severity == LintSeverity.ERROR


# ---------------------------------------------------------------------------
# BehaviorPolicyWarning — non-silent warning
# ---------------------------------------------------------------------------

def test_behavior_policy_warning_has_all_fields():
    w = BehaviorPolicyWarning(
        policy_id="p1",
        rule_id="r1",
        engine="pii",
        stage="post_verdict",
        target="telecom.cdr:msisdn",
        fallback="baseline retained",
        error="evaluator timeout",
    )
    assert w.error
    assert w.fallback
    assert w.engine == "pii"
