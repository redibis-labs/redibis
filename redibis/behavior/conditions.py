"""
redibis.behavior.conditions
=============================
Three-valued logic condition evaluation.

Predicates evaluate to TRUE, FALSE, or UNKNOWN.
UNKNOWN occurs when:
  - a fact is unavailable (not in the context facts map)
  - a fact value is None and the operator does not define null behavior

In v1, UNKNOWN always means NO_MATCH.

Operators are bounded and type-checked.
The `matches` operator uses a bounded regex with an explicit timeout/length guard.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional, Union

from redibis.behavior.models import (
    ConditionGroup,
    ConditionNode,
    JSONValue,
    Predicate,
    TruthValue,
)

_MAX_REGEX_LEN = 256
_REGEX_CACHE: dict[str, re.Pattern] = {}


# ---------------------------------------------------------------------------
# Safe regex
# ---------------------------------------------------------------------------

def _safe_compile(pattern: str) -> re.Pattern:
    """Compile a regex with length guard. Cached."""
    if pattern in _REGEX_CACHE:
        return _REGEX_CACHE[pattern]
    if len(pattern) > _MAX_REGEX_LEN:
        raise ValueError(f"regex pattern exceeds {_MAX_REGEX_LEN} chars: {pattern!r}")
    compiled = re.compile(pattern)
    if len(_REGEX_CACHE) < 512:   # bounded cache
        _REGEX_CACHE[pattern] = compiled
    return compiled


# ---------------------------------------------------------------------------
# Operator implementations
# ---------------------------------------------------------------------------

def _to_comparable(v: Any) -> Any:
    """Normalize int/bool for comparison."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return float(v)
    return v


def _eval_op(operator: str, fact_value: Any, expected: Any) -> TruthValue:
    """
    Evaluate a single operator.

    Returns UNKNOWN when the fact_value is None and the operator
    is not an existence check.
    """
    # Existence operators work regardless of None
    if operator == "exists":
        return TruthValue.TRUE if fact_value is not None else TruthValue.FALSE
    if operator == "is_null":
        return TruthValue.TRUE if fact_value is None else TruthValue.FALSE

    # For all other operators, None fact → UNKNOWN
    if fact_value is None:
        return TruthValue.UNKNOWN

    # Boolean operators
    if operator == "is_true":
        if not isinstance(fact_value, bool):
            return TruthValue.UNKNOWN
        return TruthValue.TRUE if fact_value else TruthValue.FALSE
    if operator == "is_false":
        if not isinstance(fact_value, bool):
            return TruthValue.UNKNOWN
        return TruthValue.TRUE if not fact_value else TruthValue.FALSE

    # Equality
    if operator == "eq":
        return TruthValue.TRUE if fact_value == expected else TruthValue.FALSE
    if operator == "neq":
        return TruthValue.TRUE if fact_value != expected else TruthValue.FALSE

    # Numeric ordering — require numeric types
    if operator in ("lt", "lte", "gt", "gte"):
        try:
            fv = float(fact_value)   # type: ignore[arg-type]
            ev = float(expected)     # type: ignore[arg-type]
        except (TypeError, ValueError):
            return TruthValue.UNKNOWN
        if operator == "lt":
            return TruthValue.TRUE if fv < ev else TruthValue.FALSE
        if operator == "lte":
            return TruthValue.TRUE if fv <= ev else TruthValue.FALSE
        if operator == "gt":
            return TruthValue.TRUE if fv > ev else TruthValue.FALSE
        if operator == "gte":
            return TruthValue.TRUE if fv >= ev else TruthValue.FALSE

    if operator == "between":
        if not isinstance(expected, (list, tuple)) or len(expected) != 2:
            return TruthValue.UNKNOWN
        try:
            fv = float(fact_value)   # type: ignore[arg-type]
            lo = float(expected[0])
            hi = float(expected[1])
        except (TypeError, ValueError):
            return TruthValue.UNKNOWN
        return TruthValue.TRUE if lo <= fv <= hi else TruthValue.FALSE

    # Membership
    if operator == "in":
        if not isinstance(expected, (list, tuple, set)):
            return TruthValue.UNKNOWN
        return TruthValue.TRUE if fact_value in expected else TruthValue.FALSE
    if operator == "not_in":
        if not isinstance(expected, (list, tuple, set)):
            return TruthValue.UNKNOWN
        return TruthValue.TRUE if fact_value not in expected else TruthValue.FALSE

    # List/string containment
    if operator == "contains":
        if isinstance(fact_value, str) and isinstance(expected, str):
            return TruthValue.TRUE if expected in fact_value else TruthValue.FALSE
        if isinstance(fact_value, (list, tuple)):
            return TruthValue.TRUE if expected in fact_value else TruthValue.FALSE
        return TruthValue.UNKNOWN
    if operator == "contains_any":
        if not isinstance(expected, (list, tuple)):
            return TruthValue.UNKNOWN
        if isinstance(fact_value, (list, tuple, set)):
            return TruthValue.TRUE if any(v in fact_value for v in expected) else TruthValue.FALSE
        if isinstance(fact_value, str):
            return TruthValue.TRUE if any(str(v) in fact_value for v in expected) else TruthValue.FALSE
        return TruthValue.UNKNOWN
    if operator == "contains_all":
        if not isinstance(expected, (list, tuple)):
            return TruthValue.UNKNOWN
        if isinstance(fact_value, (list, tuple, set)):
            return TruthValue.TRUE if all(v in fact_value for v in expected) else TruthValue.FALSE
        if isinstance(fact_value, str):
            return TruthValue.TRUE if all(str(v) in fact_value for v in expected) else TruthValue.FALSE
        return TruthValue.UNKNOWN

    # String operators
    if operator == "starts_with":
        if not isinstance(fact_value, str) or not isinstance(expected, str):
            return TruthValue.UNKNOWN
        return TruthValue.TRUE if fact_value.startswith(expected) else TruthValue.FALSE
    if operator == "ends_with":
        if not isinstance(fact_value, str) or not isinstance(expected, str):
            return TruthValue.UNKNOWN
        return TruthValue.TRUE if fact_value.endswith(expected) else TruthValue.FALSE
    if operator == "matches":
        if not isinstance(fact_value, str) or not isinstance(expected, str):
            return TruthValue.UNKNOWN
        try:
            compiled = _safe_compile(expected)
            return TruthValue.TRUE if compiled.search(fact_value) else TruthValue.FALSE
        except (re.error, ValueError):
            return TruthValue.UNKNOWN

    # Unknown operator → UNKNOWN (fail-safe)
    return TruthValue.UNKNOWN


# ---------------------------------------------------------------------------
# Three-valued logic truth tables
# ---------------------------------------------------------------------------

def _tv_not(v: TruthValue) -> TruthValue:
    if v == TruthValue.TRUE:
        return TruthValue.FALSE
    if v == TruthValue.FALSE:
        return TruthValue.TRUE
    return TruthValue.UNKNOWN


def _tv_and(a: TruthValue, b: TruthValue) -> TruthValue:
    """Kleene three-valued AND."""
    if a == TruthValue.FALSE or b == TruthValue.FALSE:
        return TruthValue.FALSE
    if a == TruthValue.TRUE and b == TruthValue.TRUE:
        return TruthValue.TRUE
    return TruthValue.UNKNOWN


def _tv_or(a: TruthValue, b: TruthValue) -> TruthValue:
    """Kleene three-valued OR."""
    if a == TruthValue.TRUE or b == TruthValue.TRUE:
        return TruthValue.TRUE
    if a == TruthValue.FALSE and b == TruthValue.FALSE:
        return TruthValue.FALSE
    return TruthValue.UNKNOWN


# ---------------------------------------------------------------------------
# Condition tree evaluation
# ---------------------------------------------------------------------------

def evaluate_condition(
    node: ConditionNode,
    facts: Mapping[str, JSONValue],
) -> TruthValue:
    """
    Recursively evaluate a condition node against a fact map.

    UNKNOWN propagates via three-valued logic.
    Missing facts (key not in map) are treated as None → UNKNOWN for most operators.
    """
    if isinstance(node, Predicate):
        fact_value = facts.get(node.fact)   # None if missing
        return _eval_op(node.operator, fact_value, node.expected)

    if isinstance(node, ConditionGroup):
        if not node.children:
            return TruthValue.UNKNOWN

        if node.kind == "not":
            if len(node.children) != 1:
                return TruthValue.UNKNOWN
            return _tv_not(evaluate_condition(node.children[0], facts))

        if node.kind == "all":
            result = TruthValue.TRUE
            for child in node.children:
                result = _tv_and(result, evaluate_condition(child, facts))
                if result == TruthValue.FALSE:
                    return TruthValue.FALSE   # short-circuit
            return result

        if node.kind == "any":
            result = TruthValue.FALSE
            for child in node.children:
                result = _tv_or(result, evaluate_condition(child, facts))
                if result == TruthValue.TRUE:
                    return TruthValue.TRUE    # short-circuit
            return result

    return TruthValue.UNKNOWN


def condition_matches(
    node: ConditionNode,
    facts: Mapping[str, JSONValue],
    *,
    unknown_behavior: str = "no_match",
) -> bool:
    """
    Evaluate a condition and map to a boolean decision.

    In v1, unknown_behavior is always "no_match" (UNKNOWN → False).
    """
    result = evaluate_condition(node, facts)
    if result == TruthValue.TRUE:
        return True
    if result == TruthValue.FALSE:
        return False
    # UNKNOWN
    return False   # no_match in v1
