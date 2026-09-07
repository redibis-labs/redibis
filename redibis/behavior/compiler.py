"""
redibis.behavior.compiler
==========================
Parse, validate, normalize, and compile policy documents into immutable
BehaviorPolicy and CompiledRule objects.

Compilation pipeline:
  raw dict
    → BehaviorPolicyDocument (schema validate)
    → canonical SHA-256
    → resolve scope / authority
    → build CompiledRules (registry bindings happen in registry.py)
    → BehaviorPolicy

The compiler does NOT register policies with registries or apply them to engines.
It only produces an immutable, auditable representation.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from redibis.behavior.models import (
    Authority,
    AuthoredRule,
    BehaviorPolicy,
    CompiledRule,
    ConditionGroup,
    ConditionNode,
    ConflictPolicy,
    EffectCall,
    HookStage,
    PolicySource,
    Predicate,
    RuleScope,
    UnknownBehavior,
)
from redibis.behavior.schema import (
    AppliesToSchema,
    BehaviorPolicyDocument,
    ConditionGroupSchema,
    ConditionNodeSchema,
    EffectCallSchema,
    PolicyMetadataSchema,
    PredicateSchema,
    RuleSchema,
    canonical_sha256,
    validate_document,
    validate_document_size,
)

# ---------------------------------------------------------------------------
# Authority mapping: policy source → derived authority
# ---------------------------------------------------------------------------

_SOURCE_AUTHORITY: dict[PolicySource, Authority] = {
    PolicySource.BUILTIN:      Authority.ENGINE_NATIVE,
    PolicySource.DEPLOYMENT:   Authority.APPROVED_POLICY,
    PolicySource.TENANT:       Authority.APPROVED_POLICY,
    PolicySource.USER:         Authority.APPROVED_POLICY,
    PolicySource.LLM_PROPOSED: Authority.LLM_PROPOSAL,
}

_VALID_STAGES = {s.value for s in HookStage}
_VALID_SOURCES = {s.value for s in PolicySource}


# ---------------------------------------------------------------------------
# Condition compilation
# ---------------------------------------------------------------------------

def _compile_condition(node: Any) -> ConditionNode:
    """Recursively convert a schema condition node to a frozen model."""
    if isinstance(node, PredicateSchema):
        return Predicate(fact=node.fact, operator=node.op, expected=node.value)
    if isinstance(node, ConditionGroupSchema):
        if node.all is not None:
            children = tuple(_compile_condition(c) for c in node.all)
            return ConditionGroup(kind="all", children=children)
        if node.any is not None:
            children = tuple(_compile_condition(c) for c in node.any)
            return ConditionGroup(kind="any", children=children)
        if node.not_ is not None:
            return ConditionGroup(kind="not", children=(_compile_condition(node.not_),))
    # Fallback: dict-shaped node after JSON round-trip
    if isinstance(node, dict):
        if "fact" in node:
            return Predicate(fact=node["fact"], operator=node["op"], expected=node.get("value"))
        for k in ("all", "any", "not"):
            if k in node:
                sub = node[k]
                kids = [sub] if not isinstance(sub, list) else sub
                return ConditionGroup(kind=k, children=tuple(_compile_condition(c) for c in kids))
    raise ValueError(f"Cannot compile condition node: {node!r}")


def _compile_effects(effects: list[EffectCallSchema]) -> tuple[EffectCall, ...]:
    return tuple(
        EffectCall(effect=e.effect, params=dict(e.params))
        for e in effects
    )


def _compile_scope(applies_to: AppliesToSchema) -> RuleScope:
    # engines ← appliesTo.engine (singular); environments stay in environments.
    # Never map environments into engines — that incorrectly scopes rules out.
    return RuleScope(
        engines=(applies_to.engine,) if applies_to.engine else (),
        tables=tuple(applies_to.tables),
        columns=tuple(applies_to.columns),
        environments=tuple(applies_to.environments),
        jurisdictions=tuple(applies_to.jurisdictions),
        tenant=applies_to.tenant,
        effective_from=applies_to.effective_from,
        effective_until=applies_to.effective_until,
    )


def _derive_authority(source: PolicySource) -> Authority:
    return _SOURCE_AUTHORITY.get(source, Authority.APPROVED_POLICY)


# ---------------------------------------------------------------------------
# Rule compilation
# ---------------------------------------------------------------------------

def _compile_authored_rule(
    rule: RuleSchema,
    *,
    stage: HookStage,
    scope: RuleScope,
    authority: Authority,
    policy_id: str,
    policy_version: str,
    source_layer: str,
) -> tuple[AuthoredRule, CompiledRule]:
    """Compile one rule schema into an AuthoredRule + CompiledRule pair."""
    condition = _compile_condition(rule.when)
    effects = _compile_effects(rule.effects)

    authored = AuthoredRule(
        id=rule.id,
        when=condition,
        effects=effects,
        reason=rule.reason,
        priority=rule.priority,
        terminal=rule.terminal,
    )

    compiled = CompiledRule(
        id=rule.id,
        stage=stage,
        priority=rule.priority,
        scope=scope,
        condition=condition,
        effects=effects,
        reason=rule.reason,
        terminal=rule.terminal,
        authority=authority,
        on_unknown=UnknownBehavior.NO_MATCH,
        conflict_policy=ConflictPolicy.REVIEW,
        source_layer=source_layer,
        policy_id=policy_id,
        policy_version=policy_version,
    )

    return authored, compiled


# ---------------------------------------------------------------------------
# Document compilation
# ---------------------------------------------------------------------------

def compile_policy(
    raw: dict,
    *,
    source: PolicySource = PolicySource.USER,
    source_layer: str = "",
    raw_bytes: Optional[bytes] = None,
) -> BehaviorPolicy:
    """
    Compile a raw policy dict into an immutable BehaviorPolicy.

    Parameters
    ----------
    raw:
        Parsed YAML/JSON as a Python dict.
    source:
        Origin of the policy (determines derived authority).
    source_layer:
        Optional string identifying the config layer (e.g. "global", "table").
    raw_bytes:
        Original serialized bytes for size validation. If None, skipped.

    Returns
    -------
    BehaviorPolicy
        Immutable, compiled, SHA-identified policy.

    Raises
    ------
    pydantic.ValidationError
        If the document fails schema validation.
    ValueError
        If the stage or source is unrecognized.
    """
    if raw_bytes is not None:
        validate_document_size(raw_bytes)

    doc: BehaviorPolicyDocument = validate_document(raw)

    # Derive SHA from the canonical normalized document
    content_sha256 = canonical_sha256(doc)

    # Resolve stage
    stage_str = doc.appliesTo.stage
    if stage_str not in _VALID_STAGES:
        raise ValueError(
            f"Unknown stage {stage_str!r}; valid: {sorted(_VALID_STAGES)}"
        )
    stage = HookStage(stage_str)

    scope = _compile_scope(doc.appliesTo)
    authority = _derive_authority(source)

    authored_rules: list[AuthoredRule] = []
    for rule_schema in doc.rules:
        authored, _ = _compile_authored_rule(
            rule_schema,
            stage=stage,
            scope=scope,
            authority=authority,
            policy_id=doc.metadata.id,
            policy_version=doc.metadata.version,
            source_layer=source_layer,
        )
        authored_rules.append(authored)

    return BehaviorPolicy(
        id=doc.metadata.id,
        version=doc.metadata.version,
        description=doc.metadata.description,
        engine=doc.appliesTo.engine,
        stage=stage,
        scope=scope,
        rules=tuple(authored_rules),
        content_sha256=content_sha256,
        source=source,
        owner=doc.metadata.owner,
    )


def compile_rules_for_evaluation(
    policy: BehaviorPolicy,
    *,
    source_layer: str = "",
) -> tuple[CompiledRule, ...]:
    """
    Produce CompiledRules from a BehaviorPolicy for runtime evaluation.

    Called by the evaluator or policy set builder.
    """
    authority = _derive_authority(policy.source)
    compiled: list[CompiledRule] = []
    for rule in policy.rules:
        compiled.append(
            CompiledRule(
                id=rule.id,
                stage=policy.stage,
                priority=rule.priority,
                scope=policy.scope,
                condition=rule.when,
                effects=rule.effects,
                reason=rule.reason,
                terminal=rule.terminal,
                authority=authority,
                on_unknown=UnknownBehavior.NO_MATCH,
                conflict_policy=ConflictPolicy.REVIEW,
                source_layer=source_layer or policy.source.value,
                policy_id=policy.id,
                policy_version=policy.version,
            )
        )
    return tuple(compiled)
