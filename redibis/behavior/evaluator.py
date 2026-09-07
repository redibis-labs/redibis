"""
redibis.behavior.evaluator
===========================
Priority-ordered rule evaluator with three-valued logic and full trace.

Evaluation semantics
--------------------
1. Filter rules by scope (engine, stage, table, column, environment, etc.)
2. Sort by priority (descending) then stable fully-qualified rule ID.
3. Evaluate conditions; UNKNOWN → no_match (v1 fixed).
4. Collect effects from matching rules.
5. Stop collecting effects when a matching rule has terminal=True.
6. Return all rules traces (including non-matching, out-of-scope, terminated).
"""

from __future__ import annotations

import fnmatch
import time
from typing import Any, Callable, Mapping, Optional, Sequence

from redibis.behavior.conditions import condition_matches, evaluate_condition
from redibis.behavior.models import (
    Authority,
    BehaviorCapabilities,
    BehaviorContext,
    BehaviorPatch,
    CompiledRule,
    EffectCall,
    HookStage,
    JSONValue,
    PatchOperation,
    RuleScope,
    RuleTrace,
    RuleTraceStatus,
    TruthValue,
)


# ---------------------------------------------------------------------------
# Scope matching
# ---------------------------------------------------------------------------

def _matches_scope(rule: CompiledRule, ctx: BehaviorContext) -> bool:
    """
    Return True if the rule's scope includes this context.

    Empty scope tuples mean "any" (no restriction).
    Table and column patterns use bounded glob semantics.
    """
    scope = rule.scope

    # Engine
    if scope.engines and ctx.engine not in scope.engines:
        return False

    # Environments
    if scope.environments and ctx.environment not in scope.environments:
        return False

    # Table — glob match
    if scope.tables:
        table = ctx.table or ""
        if not any(fnmatch.fnmatch(table, pat) for pat in scope.tables):
            return False

    # Column — glob match
    if scope.columns:
        column = ctx.column or ""
        if not any(fnmatch.fnmatch(column, pat) for pat in scope.columns):
            return False

    # Jurisdictions
    if scope.jurisdictions and ctx.jurisdiction not in scope.jurisdictions:
        return False

    # Tenant
    if scope.tenant is not None and ctx.tenant != scope.tenant:
        return False

    return True


# ---------------------------------------------------------------------------
# Effect application (returns patch proposals)
# ---------------------------------------------------------------------------

def _apply_effect(
    effect: EffectCall,
    ctx: BehaviorContext,
    registry,  # Optional[BehaviorRegistry]
) -> BehaviorPatch:
    """
    Dispatch effect to a registered handler or build a generic patch.

    If no handler is registered, returns a minimal patch carrying the
    effect ID and params as a "set" operation. The reducer will validate
    whether this operation is permitted by the adapter capabilities.
    """
    if registry is not None:
        action = registry.get_action(effect.effect)
        if action is not None:
            try:
                return action.apply(ctx, effect.params)
            except Exception as exc:
                raise RuntimeError(
                    f"Effect handler {effect.effect!r} raised: {exc}"
                ) from exc

    # Built-in no-op patch: pass the effect through as an operation.
    # The reducer will decide if it's applicable.
    op_name = effect.effect
    # Map well-known effects to structured operations
    ops: list[PatchOperation] = []
    if effect.effect == "core.verdict.set_entity":
        ops.append(PatchOperation(
            op="set",
            path="verdict.entity",
            value=effect.params.get("entity"),
            authority=ctx.capabilities.allowed_patch_operations and Authority.APPROVED_POLICY or Authority.APPROVED_POLICY,
            reason="",   # filled from rule reason in evaluator
        ))
    elif effect.effect == "core.verdict.set_detected":
        ops.append(PatchOperation(
            op="set",
            path="verdict.detected",
            value=effect.params.get("detected"),
            authority=Authority.APPROVED_POLICY,
            reason="",
        ))
    elif effect.effect == "core.verdict.set_confidence":
        ops.append(PatchOperation(
            op="set",
            path="verdict.confidence",
            value=effect.params.get("confidence"),
            authority=Authority.APPROVED_POLICY,
            reason="",
        ))
    elif effect.effect == "core.review.require":
        return BehaviorPatch(
            require_review=True,
            review_role=str(effect.params.get("role") or "steward"),
            reasons=(),
        )
    elif effect.effect == "core.review.add_reason":
        reason_str = str(effect.params.get("reason") or "")
        return BehaviorPatch(reasons=(reason_str,) if reason_str else ())
    elif effect.effect == "core.threshold.set_for_run":
        ops.append(PatchOperation(
            op="set",
            path=f"threshold.{effect.params.get('threshold', 'unknown')}",
            value=effect.params.get("value"),
            authority=Authority.APPROVED_POLICY,
            reason="",
        ))
    else:
        # Unknown/custom effect: pass through as a generic operation
        ops.append(PatchOperation(
            op="set",
            path=f"effect.{effect.effect}",
            value=dict(effect.params),
            authority=Authority.APPROVED_POLICY,
            reason="",
        ))

    return BehaviorPatch(operations=tuple(ops))


def _annotate_operations(
    patch: BehaviorPatch, rule_id: str, policy_id: str, reason: str
) -> BehaviorPatch:
    """Fill in rule/policy/reason on operations where missing."""
    updated_ops = tuple(
        PatchOperation(
            op=op.op,
            path=op.path,
            value=op.value,
            authority=op.authority,
            reason=op.reason or reason,
            rule_id=op.rule_id or rule_id,
            policy_id=op.policy_id or policy_id,
        )
        for op in patch.operations
    )
    return BehaviorPatch(
        operations=updated_ops,
        require_review=patch.require_review,
        review_role=patch.review_role,
        reasons=patch.reasons,
    )


# ---------------------------------------------------------------------------
# Patch merging
# ---------------------------------------------------------------------------

def _merge_patches(patches: list[BehaviorPatch]) -> BehaviorPatch:
    """Combine multiple BehaviorPatch objects into one."""
    all_ops: list[PatchOperation] = []
    require_review = False
    review_role: Optional[str] = None
    reasons: list[str] = []

    for p in patches:
        all_ops.extend(p.operations)
        if p.require_review:
            require_review = True
        if p.review_role:
            review_role = review_role or p.review_role
        reasons.extend(p.reasons)

    return BehaviorPatch(
        operations=tuple(all_ops),
        require_review=require_review,
        review_role=review_role,
        reasons=tuple(reasons),
    )


# ---------------------------------------------------------------------------
# Main evaluator
# ---------------------------------------------------------------------------

class PolicyEvaluator:
    """
    Evaluates a sorted set of CompiledRules against a BehaviorContext.

    Returns a list of RuleTraces and the proposed BehaviorPatch.
    """

    def __init__(self, registry=None) -> None:
        self._registry = registry

    def evaluate(
        self,
        rules: Sequence[CompiledRule],
        ctx: BehaviorContext,
        *,
        stage: HookStage,
    ) -> tuple[list[RuleTrace], BehaviorPatch]:
        """
        Evaluate all rules applicable to ctx at the given stage.

        Parameters
        ----------
        rules:
            Pre-sorted (desc priority, stable ID) CompiledRules.
        ctx:
            Evaluation context (privacy-safe facts).
        stage:
            The current hook stage.

        Returns
        -------
        (traces, proposed_patch)
            traces: one entry per rule (sorted as evaluated).
            proposed_patch: accumulated BehaviorPatch from matching rules.
        """
        traces: list[RuleTrace] = []
        matching_patches: list[BehaviorPatch] = []
        terminated = False

        for rule in rules:
            # Skip rules not applicable to this stage
            if rule.stage != stage:
                traces.append(RuleTrace(
                    rule_id=rule.id,
                    policy_id=rule.policy_id,
                    priority=rule.priority,
                    status=RuleTraceStatus.OUT_OF_SCOPE,
                    reason=rule.reason,
                ))
                continue

            # Skip rules whose scope doesn't match
            if not _matches_scope(rule, ctx):
                traces.append(RuleTrace(
                    rule_id=rule.id,
                    policy_id=rule.policy_id,
                    priority=rule.priority,
                    status=RuleTraceStatus.OUT_OF_SCOPE,
                    reason=rule.reason,
                ))
                continue

            # After a terminal match, mark remaining as terminated
            if terminated:
                traces.append(RuleTrace(
                    rule_id=rule.id,
                    policy_id=rule.policy_id,
                    priority=rule.priority,
                    status=RuleTraceStatus.TERMINATED,
                    reason=rule.reason,
                ))
                continue

            # Evaluate condition
            condition_result = evaluate_condition(rule.condition, ctx.facts)

            if condition_result == TruthValue.UNKNOWN:
                traces.append(RuleTrace(
                    rule_id=rule.id,
                    policy_id=rule.policy_id,
                    priority=rule.priority,
                    status=RuleTraceStatus.UNKNOWN_NO_MATCH,
                    reason=rule.reason,
                ))
                continue

            if condition_result == TruthValue.FALSE:
                traces.append(RuleTrace(
                    rule_id=rule.id,
                    policy_id=rule.policy_id,
                    priority=rule.priority,
                    status=RuleTraceStatus.CONDITION_FALSE,
                    reason=rule.reason,
                ))
                continue

            # MATCHED — execute effects
            effect_patches: list[BehaviorPatch] = []
            effect_error: Optional[str] = None

            for effect in rule.effects:
                try:
                    ep = _apply_effect(effect, ctx, self._registry)
                    ep = _annotate_operations(ep, rule.id, rule.policy_id, rule.reason)
                    effect_patches.append(ep)
                except Exception as exc:
                    effect_error = str(exc)
                    break

            if effect_error:
                traces.append(RuleTrace(
                    rule_id=rule.id,
                    policy_id=rule.policy_id,
                    priority=rule.priority,
                    status=RuleTraceStatus.EFFECT_ERROR,
                    reason=rule.reason,
                    error=effect_error,
                ))
                continue

            rule_patch = _merge_patches(effect_patches)
            matching_patches.append(rule_patch)

            traces.append(RuleTrace(
                rule_id=rule.id,
                policy_id=rule.policy_id,
                priority=rule.priority,
                status=RuleTraceStatus.MATCHED,
                reason=rule.reason,
                patch=rule_patch,
            ))

            if rule.terminal:
                terminated = True

        proposed = _merge_patches(matching_patches)
        return traces, proposed


def sort_rules(rules: Sequence[CompiledRule]) -> list[CompiledRule]:
    """Sort rules by priority (descending) then fully-qualified ID (ascending)."""
    return sorted(rules, key=lambda r: (-r.priority, r.policy_id, r.id))
