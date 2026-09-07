"""
redibis.behavior.lint
======================
V1 policy linter — structural and semantic checks that run before activation.

Lint findings gate policy activation:
  - ERROR / conflict severity blocks activation
  - SHADOWED requires explicit acknowledgement + simulation
  - WARNING requires simulation

All checks run against compiled models so the linter sees normalized data.
"""

from __future__ import annotations

from typing import Optional, Sequence

from redibis.behavior.models import (
    AuthoredRule,
    BehaviorPolicy,
    ConditionGroup,
    ConditionNode,
    LintFinding,
    LintSeverity,
    Predicate,
    TruthValue,
)

# Stable finding IDs
FIND_MISSING_REASON         = "MISSING_REASON"
FIND_DUPLICATE_RULE_ID      = "DUPLICATE_RULE_ID"
FIND_SHADOWED_RULE          = "SHADOWED_RULE"
FIND_TERMINAL_SHADOWS       = "TERMINAL_SHADOWS_LOWER"
FIND_SAME_PRIORITY_CONFLICT = "SAME_PRIORITY_CONFLICT"
FIND_UNKNOWN_ONLY_RULE      = "UNKNOWN_ONLY_RULE"
FIND_DUPLICATE_CONDITION    = "DUPLICATE_CONDITION"
FIND_EMPTY_EFFECTS          = "EMPTY_EFFECTS"
FIND_BROAD_BEFORE_NARROW    = "BROAD_BEFORE_NARROW"


# ---------------------------------------------------------------------------
# Condition complexity helpers
# ---------------------------------------------------------------------------

def _condition_predicates(node: ConditionNode) -> list[Predicate]:
    """Flatten all Predicates out of a condition tree."""
    if isinstance(node, Predicate):
        return [node]
    if isinstance(node, ConditionGroup):
        result: list[Predicate] = []
        for child in node.children:
            result.extend(_condition_predicates(child))
        return result
    return []


def _condition_facts(node: ConditionNode) -> frozenset[str]:
    """Return the set of fact names referenced by this condition."""
    return frozenset(p.fact for p in _condition_predicates(node))


def _condition_key(node: ConditionNode) -> str:
    """Stable string key for condition equality detection."""
    if isinstance(node, Predicate):
        return f"pred:{node.fact}:{node.operator}:{node.expected!r}"
    if isinstance(node, ConditionGroup):
        children_key = "|".join(sorted(_condition_key(c) for c in node.children))
        return f"grp:{node.kind}:{children_key}"
    return repr(node)


def _condition_is_superset(broad: ConditionNode, narrow: ConditionNode) -> bool:
    """
    Heuristic: is `broad` a subset of conditions (fewer predicates)?

    A rule is considered broader if its condition references a strict subset of
    the facts used by the narrower rule.
    """
    broad_facts = _condition_facts(broad)
    narrow_facts = _condition_facts(narrow)
    if not broad_facts:
        return False
    return broad_facts < narrow_facts


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def _check_missing_reason(policy: BehaviorPolicy) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for rule in policy.rules:
        if not rule.reason or not rule.reason.strip():
            findings.append(LintFinding(
                finding_id=FIND_MISSING_REASON,
                severity=LintSeverity.ERROR,
                policy_id=policy.id,
                rule_id=rule.id,
                message=f"Rule {rule.id!r} is missing a mandatory non-empty reason.",
                suggestion="Add a non-empty reason field explaining why this rule exists.",
            ))
    return findings


def _check_duplicate_rule_ids(policy: BehaviorPolicy) -> list[LintFinding]:
    seen: set[str] = set()
    findings: list[LintFinding] = []
    for rule in policy.rules:
        if rule.id in seen:
            findings.append(LintFinding(
                finding_id=FIND_DUPLICATE_RULE_ID,
                severity=LintSeverity.ERROR,
                policy_id=policy.id,
                rule_id=rule.id,
                message=f"Duplicate rule id {rule.id!r} in policy {policy.id!r}.",
                suggestion="Each rule within a policy must have a unique id.",
            ))
        seen.add(rule.id)
    return findings


def _check_empty_effects(policy: BehaviorPolicy) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for rule in policy.rules:
        if not rule.effects:
            findings.append(LintFinding(
                finding_id=FIND_EMPTY_EFFECTS,
                severity=LintSeverity.ERROR,
                policy_id=policy.id,
                rule_id=rule.id,
                message=f"Rule {rule.id!r} has no effects — it would match but do nothing.",
                suggestion="Add at least one effect or remove the rule.",
            ))
    return findings


def _check_terminal_shadows(policy: BehaviorPolicy) -> list[LintFinding]:
    """Warn when a terminal rule shadows lower-priority rules."""
    findings: list[LintFinding] = []
    sorted_rules = sorted(policy.rules, key=lambda r: (-r.priority, r.id))
    terminal_indices: list[int] = []
    for i, rule in enumerate(sorted_rules):
        # Check if this rule is shadowed by a previous terminal
        for ti in terminal_indices:
            higher = sorted_rules[ti]
            findings.append(LintFinding(
                finding_id=FIND_TERMINAL_SHADOWS,
                severity=LintSeverity.WARNING,
                policy_id=policy.id,
                rule_id=rule.id,
                message=(
                    f"Rule {rule.id!r} (priority={rule.priority}) may never execute "
                    f"because terminal rule {higher.id!r} (priority={higher.priority}) "
                    "evaluates first and stops further rules on match."
                ),
                suggestion=(
                    "Increase the priority of the more-specific rule, or remove "
                    "terminal=true from the higher-priority rule."
                ),
            ))
        if rule.terminal:
            terminal_indices.append(i)
    return findings


def _check_same_priority_conflicts(policy: BehaviorPolicy) -> list[LintFinding]:
    """
    Detect rules at the same priority that target the same fact path with
    incompatible scalar effects.
    """
    findings: list[LintFinding] = []
    by_priority: dict[int, list[AuthoredRule]] = {}
    for rule in policy.rules:
        by_priority.setdefault(rule.priority, []).append(rule)

    for prio, rules in by_priority.items():
        if len(rules) < 2:
            continue
        # Look for rules with overlapping effect paths
        effect_sets: list[tuple[str, set[str]]] = []
        for rule in rules:
            paths = {e.effect for e in rule.effects}
            effect_sets.append((rule.id, paths))
        for i in range(len(effect_sets)):
            for j in range(i + 1, len(effect_sets)):
                rid_a, paths_a = effect_sets[i]
                rid_b, paths_b = effect_sets[j]
                shared = paths_a & paths_b
                if shared:
                    findings.append(LintFinding(
                        finding_id=FIND_SAME_PRIORITY_CONFLICT,
                        severity=LintSeverity.WARNING,
                        policy_id=policy.id,
                        rule_id=rid_a,
                        message=(
                            f"Rules {rid_a!r} and {rid_b!r} share priority {prio} "
                            f"and both target effect(s) {sorted(shared)} — "
                            "conflicting scalar effects will be routed to review."
                        ),
                        suggestion=(
                            "Assign different priorities or use compatible additive effects."
                        ),
                    ))
    return findings


def _check_shadowed_rules(policy: BehaviorPolicy) -> list[LintFinding]:
    """
    Heuristic: a rule is potentially shadowed if a higher-priority rule
    references a superset of its conditions (fewer facts = broader match).
    """
    findings: list[LintFinding] = []
    sorted_rules = sorted(policy.rules, key=lambda r: (-r.priority, r.id))
    for i, narrow in enumerate(sorted_rules):
        for j in range(i):
            broad = sorted_rules[j]
            if broad.priority >= narrow.priority and _condition_is_superset(
                broad.when, narrow.when
            ):
                findings.append(LintFinding(
                    finding_id=FIND_SHADOWED_RULE,
                    severity=LintSeverity.WARNING,
                    policy_id=policy.id,
                    rule_id=narrow.id,
                    message=(
                        f"Rule {narrow.id!r} (priority={narrow.priority}) may be shadowed "
                        f"by broader rule {broad.id!r} (priority={broad.priority}) which "
                        "uses fewer conditions and evaluates first."
                    ),
                    suggestion=(
                        "Increase the priority of the more-specific rule, "
                        "add terminal=true to the narrower rule, or "
                        "explicitly acknowledge this ordering."
                    ),
                ))
    return findings


def _check_duplicate_conditions(policy: BehaviorPolicy) -> list[LintFinding]:
    """Detect rules with identical condition trees."""
    findings: list[LintFinding] = []
    seen_cond: dict[str, str] = {}   # condition_key -> first rule_id
    for rule in policy.rules:
        key = _condition_key(rule.when)
        if key in seen_cond:
            findings.append(LintFinding(
                finding_id=FIND_DUPLICATE_CONDITION,
                severity=LintSeverity.WARNING,
                policy_id=policy.id,
                rule_id=rule.id,
                message=(
                    f"Rule {rule.id!r} has the same condition as rule "
                    f"{seen_cond[key]!r}. One will never produce a different "
                    "outcome than the other (unless priority ordering matters)."
                ),
                suggestion=(
                    "Merge the effects into one rule or differentiate the conditions."
                ),
            ))
        else:
            seen_cond[key] = rule.id
    return findings


# ---------------------------------------------------------------------------
# Main linting entry point
# ---------------------------------------------------------------------------

def lint_policy(
    policy: BehaviorPolicy,
    *,
    registry=None,
) -> list[LintFinding]:
    """
    Run all v1 lint checks against a compiled policy.

    Parameters
    ----------
    policy:
        A compiled BehaviorPolicy (from compiler.compile_policy).
    registry:
        Optional behavior registry. When provided, registry-aware checks
        run (unknown facts/effects per engine+stage).

    Returns
    -------
    list[LintFinding]
        Sorted by severity (errors first), then policy_id, then rule_id.
    """
    findings: list[LintFinding] = []
    findings.extend(_check_missing_reason(policy))
    findings.extend(_check_duplicate_rule_ids(policy))
    findings.extend(_check_empty_effects(policy))
    findings.extend(_check_terminal_shadows(policy))
    findings.extend(_check_same_priority_conflicts(policy))
    findings.extend(_check_shadowed_rules(policy))
    findings.extend(_check_duplicate_conditions(policy))

    if registry is not None:
        findings.extend(_check_registry_references(policy, registry))

    # Sort: ERROR first, then WARNING, then INFO; stable by rule_id
    _order = {LintSeverity.ERROR: 0, LintSeverity.WARNING: 1, LintSeverity.INFO: 2}
    findings.sort(key=lambda f: (_order.get(f.severity, 9), f.rule_id or ""))
    return findings


def _check_registry_references(
    policy: BehaviorPolicy, registry: Any
) -> list[LintFinding]:
    """
    Check that every fact and effect referenced in the policy is registered
    and supported at the declared engine + stage.
    """
    from redibis.behavior.registry import BehaviorRegistry

    findings: list[LintFinding] = []
    reg: BehaviorRegistry = registry

    for rule in policy.rules:
        # Check facts
        for pred in _condition_predicates(rule.when):
            if not reg.has_fact(pred.fact):
                findings.append(LintFinding(
                    finding_id="UNKNOWN_FACT",
                    severity=LintSeverity.ERROR,
                    policy_id=policy.id,
                    rule_id=rule.id,
                    message=f"Fact {pred.fact!r} is not registered.",
                    suggestion="Use a registered fact from the behavior catalog.",
                ))
        # Check effects
        for effect in rule.effects:
            if not reg.has_effect(effect.effect):
                findings.append(LintFinding(
                    finding_id="UNKNOWN_EFFECT",
                    severity=LintSeverity.ERROR,
                    policy_id=policy.id,
                    rule_id=rule.id,
                    message=f"Effect {effect.effect!r} is not registered.",
                    suggestion="Use a registered effect from the behavior catalog.",
                ))

    return findings


def has_blocking_findings(findings: list[LintFinding]) -> bool:
    """Return True if any finding blocks policy activation."""
    return any(f.severity == LintSeverity.ERROR for f in findings)
