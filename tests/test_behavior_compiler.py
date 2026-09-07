"""Tests for behavior compiler and linter."""

from __future__ import annotations

import pytest

from redibis.behavior.compiler import compile_policy, compile_rules_for_evaluation
from redibis.behavior.lint import (
    FIND_MISSING_REASON,
    FIND_SHADOWED_RULE,
    FIND_TERMINAL_SHADOWS,
    FIND_SAME_PRIORITY_CONFLICT,
    FIND_DUPLICATE_CONDITION,
    has_blocking_findings,
    lint_policy,
)
from redibis.behavior.models import (
    Authority,
    HookStage,
    LintSeverity,
    PolicySource,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

MINIMAL_DOC = {
    "apiVersion": "redibis.io/behavior-policy/v1",
    "kind": "BehaviorPolicy",
    "metadata": {"id": "test-policy", "version": "1.0.0", "description": "Test"},
    "appliesTo": {"engine": "pii", "stage": "post_verdict"},
    "rules": [
        {
            "id": "rule-1",
            "when": {"fact": "column.name", "op": "matches", "value": "(?i)lac"},
            "effects": [
                {"effect": "core.verdict.set_entity", "params": {"entity": "NETWORK_ID"}}
            ],
            "reason": "LAC is a network identifier.",
        }
    ],
}


# ---------------------------------------------------------------------------
# Compiler
# ---------------------------------------------------------------------------

def test_compile_minimal_policy():
    policy = compile_policy(MINIMAL_DOC)
    assert policy.id == "test-policy"
    assert policy.version == "1.0.0"
    assert policy.stage == HookStage.POST_VERDICT
    assert len(policy.rules) == 1
    assert policy.rules[0].reason == "LAC is a network identifier."
    assert len(policy.content_sha256) == 64


def test_compile_policy_default_source():
    policy = compile_policy(MINIMAL_DOC, source=PolicySource.USER)
    assert policy.source == PolicySource.USER


def test_compile_policy_builtin_source():
    policy = compile_policy(MINIMAL_DOC, source=PolicySource.BUILTIN)
    assert policy.source == PolicySource.BUILTIN


def test_compile_policy_sha256_stable():
    p1 = compile_policy(MINIMAL_DOC)
    p2 = compile_policy(MINIMAL_DOC)
    assert p1.content_sha256 == p2.content_sha256


def test_compile_policy_sha256_changes_on_reason_change():
    p1 = compile_policy(MINIMAL_DOC)
    changed = dict(MINIMAL_DOC)
    rule = dict(MINIMAL_DOC["rules"][0], reason="Different reason.")
    changed["rules"] = [rule]
    p2 = compile_policy(changed)
    assert p1.content_sha256 != p2.content_sha256


def test_compile_policy_unknown_stage_rejected():
    bad = dict(MINIMAL_DOC)
    bad["appliesTo"] = dict(bad["appliesTo"], stage="invalid_stage")
    with pytest.raises(ValueError, match="Unknown stage"):
        compile_policy(bad)


def test_compile_rules_for_evaluation():
    policy = compile_policy(MINIMAL_DOC)
    compiled = compile_rules_for_evaluation(policy)
    assert len(compiled) == 1
    cr = compiled[0]
    assert cr.policy_id == "test-policy"
    assert cr.stage == HookStage.POST_VERDICT
    assert cr.terminal is False


def test_compiled_rule_enforcement_fields():
    """Authority, on_unknown, conflict_policy are set by compiler, not authored."""
    policy = compile_policy(MINIMAL_DOC, source=PolicySource.USER)
    compiled = compile_rules_for_evaluation(policy)
    cr = compiled[0]
    from redibis.behavior.models import UnknownBehavior, ConflictPolicy
    assert cr.on_unknown == UnknownBehavior.NO_MATCH
    assert cr.conflict_policy == ConflictPolicy.REVIEW
    # authority derived from USER source
    assert cr.authority == Authority.APPROVED_POLICY


def test_compile_nested_condition():
    doc = dict(MINIMAL_DOC)
    doc["rules"] = [
        {
            "id": "nested",
            "when": {
                "all": [
                    {"fact": "column.name", "op": "matches", "value": "lac"},
                    {"fact": "verdict.entity", "op": "eq", "value": "PHONE_NUMBER"},
                ]
            },
            "effects": [
                {"effect": "core.verdict.set_entity", "params": {"entity": "NETWORK_ID"}}
            ],
            "reason": "Nested condition test.",
        }
    ]
    policy = compile_policy(doc)
    assert policy.rules[0].id == "nested"
    from redibis.behavior.models import ConditionGroup
    assert isinstance(policy.rules[0].when, ConditionGroup)


def test_compile_size_limit():
    from redibis.behavior.schema import MAX_DOCUMENT_BYTES
    big_bytes = b"x" * (MAX_DOCUMENT_BYTES + 1)
    with pytest.raises(ValueError, match="maximum size"):
        compile_policy(MINIMAL_DOC, raw_bytes=big_bytes)


# ---------------------------------------------------------------------------
# Linter
# ---------------------------------------------------------------------------

def test_lint_valid_policy_no_errors():
    policy = compile_policy(MINIMAL_DOC)
    findings = lint_policy(policy)
    blocking = [f for f in findings if f.severity == LintSeverity.ERROR]
    assert not blocking


def test_lint_terminal_shadowing_warning():
    """A terminal rule at priority 200 warns about rules at priority 100."""
    doc = dict(MINIMAL_DOC)
    doc["rules"] = [
        {
            "id": "terminal-first",
            "when": {"fact": "column.name", "op": "matches", "value": "lac"},
            "effects": [
                {"effect": "core.verdict.set_entity", "params": {"entity": "NETWORK_ID"}}
            ],
            "reason": "High-priority terminal rule.",
            "priority": 200,
            "terminal": True,
        },
        {
            "id": "shadowed-lower",
            "when": {"fact": "column.name", "op": "eq", "value": "tac"},
            "effects": [
                {"effect": "core.verdict.set_entity", "params": {"entity": "NETWORK_ID"}}
            ],
            "reason": "Lower priority rule potentially shadowed.",
            "priority": 100,
        },
    ]
    policy = compile_policy(doc)
    findings = lint_policy(policy)
    terminal_findings = [f for f in findings if f.finding_id == FIND_TERMINAL_SHADOWS]
    assert terminal_findings, "Expected TERMINAL_SHADOWS finding"
    assert any(f.rule_id == "shadowed-lower" for f in terminal_findings)


def test_lint_duplicate_condition_warning():
    doc = dict(MINIMAL_DOC)
    doc["rules"] = [
        {
            "id": "rule-a",
            "when": {"fact": "column.name", "op": "eq", "value": "msisdn"},
            "effects": [{"effect": "core.verdict.set_entity", "params": {"entity": "PHONE_NUMBER"}}],
            "reason": "First rule.",
        },
        {
            "id": "rule-b",
            "when": {"fact": "column.name", "op": "eq", "value": "msisdn"},
            "effects": [{"effect": "core.review.require", "params": {}}],
            "reason": "Second rule with same condition.",
        },
    ]
    policy = compile_policy(doc)
    findings = lint_policy(policy)
    dup_findings = [f for f in findings if f.finding_id == FIND_DUPLICATE_CONDITION]
    assert dup_findings


def test_lint_has_blocking_findings():
    from redibis.behavior.models import BehaviorPolicy, LintFinding, LintSeverity, HookStage, RuleScope, PolicySource
    policy = compile_policy(MINIMAL_DOC)
    findings = [LintFinding(
        finding_id="MISSING_REASON", severity=LintSeverity.ERROR,
        policy_id="p", rule_id="r", message="test error",
    )]
    assert has_blocking_findings(findings)
    assert not has_blocking_findings([])
