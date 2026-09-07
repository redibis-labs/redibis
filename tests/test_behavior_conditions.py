"""Tests for redibis.behavior.conditions — three-valued logic."""

from __future__ import annotations

import pytest

from redibis.behavior.conditions import (
    condition_matches,
    evaluate_condition,
)
from redibis.behavior.models import (
    ConditionGroup,
    Predicate,
    TruthValue,
)


# ---------------------------------------------------------------------------
# Atomic operator tests
# ---------------------------------------------------------------------------

def _pred(fact, op, expected=None):
    return Predicate(fact=fact, operator=op, expected=expected)


def _eval(node, facts):
    return evaluate_condition(node, facts)


def test_eq_true():
    assert _eval(_pred("x", "eq", "foo"), {"x": "foo"}) == TruthValue.TRUE


def test_eq_false():
    assert _eval(_pred("x", "eq", "foo"), {"x": "bar"}) == TruthValue.FALSE


def test_neq():
    assert _eval(_pred("x", "neq", "foo"), {"x": "bar"}) == TruthValue.TRUE
    assert _eval(_pred("x", "neq", "foo"), {"x": "foo"}) == TruthValue.FALSE


def test_lt():
    assert _eval(_pred("x", "lt", 5.0), {"x": 3.0}) == TruthValue.TRUE
    assert _eval(_pred("x", "lt", 5.0), {"x": 5.0}) == TruthValue.FALSE
    assert _eval(_pred("x", "lt", 5.0), {"x": 6.0}) == TruthValue.FALSE


def test_lte():
    assert _eval(_pred("x", "lte", 5.0), {"x": 5.0}) == TruthValue.TRUE
    assert _eval(_pred("x", "lte", 5.0), {"x": 6.0}) == TruthValue.FALSE


def test_gt():
    assert _eval(_pred("x", "gt", 0.8), {"x": 0.9}) == TruthValue.TRUE
    assert _eval(_pred("x", "gt", 0.8), {"x": 0.5}) == TruthValue.FALSE


def test_gte():
    assert _eval(_pred("x", "gte", 0.8), {"x": 0.8}) == TruthValue.TRUE
    assert _eval(_pred("x", "gte", 0.8), {"x": 0.79}) == TruthValue.FALSE


def test_between():
    assert _eval(_pred("x", "between", [0.0, 1.0]), {"x": 0.5}) == TruthValue.TRUE
    assert _eval(_pred("x", "between", [0.0, 0.5]), {"x": 0.5}) == TruthValue.TRUE
    assert _eval(_pred("x", "between", [0.0, 0.4]), {"x": 0.5}) == TruthValue.FALSE


def test_in():
    assert _eval(_pred("x", "in", ["a", "b", "c"]), {"x": "b"}) == TruthValue.TRUE
    assert _eval(_pred("x", "in", ["a", "b"]), {"x": "z"}) == TruthValue.FALSE


def test_not_in():
    assert _eval(_pred("x", "not_in", ["a", "b"]), {"x": "z"}) == TruthValue.TRUE
    assert _eval(_pred("x", "not_in", ["a", "b"]), {"x": "a"}) == TruthValue.FALSE


def test_contains():
    assert _eval(_pred("x", "contains", "foo"), {"x": "foobar"}) == TruthValue.TRUE
    assert _eval(_pred("x", "contains", "baz"), {"x": "foobar"}) == TruthValue.FALSE


def test_contains_any():
    assert _eval(_pred("x", "contains_any", ["a", "b"]), {"x": ["a", "c"]}) == TruthValue.TRUE
    assert _eval(_pred("x", "contains_any", ["z"]), {"x": ["a", "b"]}) == TruthValue.FALSE


def test_contains_all():
    assert _eval(_pred("x", "contains_all", ["a", "b"]), {"x": ["a", "b", "c"]}) == TruthValue.TRUE
    assert _eval(_pred("x", "contains_all", ["a", "z"]), {"x": ["a", "b"]}) == TruthValue.FALSE


def test_starts_with():
    assert _eval(_pred("x", "starts_with", "pre"), {"x": "prefix"}) == TruthValue.TRUE
    assert _eval(_pred("x", "starts_with", "pre"), {"x": "suffix"}) == TruthValue.FALSE


def test_ends_with():
    assert _eval(_pred("x", "ends_with", "fix"), {"x": "suffix"}) == TruthValue.TRUE
    assert _eval(_pred("x", "ends_with", "pre"), {"x": "suffix"}) == TruthValue.FALSE


def test_matches():
    assert _eval(_pred("x", "matches", "(?i)lac"), {"x": "lac"}) == TruthValue.TRUE
    assert _eval(_pred("x", "matches", "(?i)lac"), {"x": "tac"}) == TruthValue.FALSE


def test_matches_regex_too_long():
    long_pattern = "a" * 300
    # Should evaluate to UNKNOWN (not raise)
    result = _eval(_pred("x", "matches", long_pattern), {"x": "abc"})
    assert result == TruthValue.UNKNOWN


def test_exists():
    assert _eval(_pred("x", "exists"), {"x": "anything"}) == TruthValue.TRUE
    assert _eval(_pred("x", "exists"), {}) == TruthValue.FALSE
    assert _eval(_pred("x", "exists"), {"x": None}) == TruthValue.FALSE


def test_is_null():
    assert _eval(_pred("x", "is_null"), {"x": None}) == TruthValue.TRUE
    assert _eval(_pred("x", "is_null"), {"x": "value"}) == TruthValue.FALSE
    assert _eval(_pred("x", "is_null"), {}) == TruthValue.TRUE


def test_is_true():
    assert _eval(_pred("x", "is_true"), {"x": True}) == TruthValue.TRUE
    assert _eval(_pred("x", "is_true"), {"x": False}) == TruthValue.FALSE
    assert _eval(_pred("x", "is_true"), {"x": "yes"}) == TruthValue.UNKNOWN


def test_is_false():
    assert _eval(_pred("x", "is_false"), {"x": False}) == TruthValue.TRUE
    assert _eval(_pred("x", "is_false"), {"x": True}) == TruthValue.FALSE


# ---------------------------------------------------------------------------
# Missing fact → UNKNOWN
# ---------------------------------------------------------------------------

def test_missing_fact_unknown():
    result = _eval(_pred("missing", "eq", "val"), {})
    assert result == TruthValue.UNKNOWN


def test_none_fact_unknown_for_non_existence_op():
    result = _eval(_pred("x", "eq", "val"), {"x": None})
    assert result == TruthValue.UNKNOWN


# ---------------------------------------------------------------------------
# Three-valued logic combinators
# ---------------------------------------------------------------------------

def test_all_true():
    cg = ConditionGroup(kind="all", children=(
        _pred("a", "eq", 1),
        _pred("b", "eq", 2),
    ))
    assert _eval(cg, {"a": 1, "b": 2}) == TruthValue.TRUE


def test_all_false_short_circuits():
    cg = ConditionGroup(kind="all", children=(
        _pred("a", "eq", 99),   # false
        _pred("b", "eq", 2),
    ))
    assert _eval(cg, {"a": 1, "b": 2}) == TruthValue.FALSE


def test_all_with_unknown():
    cg = ConditionGroup(kind="all", children=(
        _pred("a", "eq", 1),
        _pred("missing", "eq", 2),   # missing → UNKNOWN
    ))
    assert _eval(cg, {"a": 1}) == TruthValue.UNKNOWN


def test_any_true_short_circuits():
    cg = ConditionGroup(kind="any", children=(
        _pred("a", "eq", 1),   # true
        _pred("b", "eq", 99),
    ))
    assert _eval(cg, {"a": 1, "b": 2}) == TruthValue.TRUE


def test_any_false():
    cg = ConditionGroup(kind="any", children=(
        _pred("a", "eq", 99),
        _pred("b", "eq", 88),
    ))
    assert _eval(cg, {"a": 1, "b": 2}) == TruthValue.FALSE


def test_any_with_unknown():
    cg = ConditionGroup(kind="any", children=(
        _pred("a", "eq", 99),   # false
        _pred("missing", "eq", 1),   # UNKNOWN
    ))
    assert _eval(cg, {"a": 1}) == TruthValue.UNKNOWN


def test_not_inverts():
    cg = ConditionGroup(kind="not", children=(_pred("a", "eq", 1),))
    assert _eval(cg, {"a": 1}) == TruthValue.FALSE
    assert _eval(cg, {"a": 2}) == TruthValue.TRUE


def test_not_unknown_stays_unknown():
    cg = ConditionGroup(kind="not", children=(_pred("missing", "eq", 1),))
    assert _eval(cg, {}) == TruthValue.UNKNOWN


# ---------------------------------------------------------------------------
# condition_matches — boolean decision (UNKNOWN → False)
# ---------------------------------------------------------------------------

def test_condition_matches_true():
    p = _pred("x", "eq", "yes")
    assert condition_matches(p, {"x": "yes"}) is True


def test_condition_matches_false():
    p = _pred("x", "eq", "yes")
    assert condition_matches(p, {"x": "no"}) is False


def test_condition_matches_unknown_is_false():
    p = _pred("missing", "eq", "yes")
    assert condition_matches(p, {}) is False   # UNKNOWN → no_match → False
