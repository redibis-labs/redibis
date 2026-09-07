"""
Engine-neutral quality rule vocabulary + mappers to/from GE expectation names.

``QualityRuleSet`` stores GE-shaped dicts for backward compatibility; validators
translate through this module so a future non-GE backend can consume the same
canonical checks without leaking expectation type strings into the core.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from redibis.quality.rule_set import QualityRuleSet

# Canonical check ids (validator-agnostic)
CHECK_NOT_NULL = "not_null"
CHECK_UNIQUE = "unique"
CHECK_IN_SET = "in_set"
CHECK_BETWEEN = "between"
CHECK_REGEX = "regex"
CHECK_TYPE = "type"
CHECK_SQL = "sql"

_GE_TO_CANONICAL: dict[str, str] = {
    "expect_column_values_to_not_be_null": CHECK_NOT_NULL,
    "expect_column_value_lengths_to_be_between": CHECK_BETWEEN,
    "expect_column_values_to_be_between": CHECK_BETWEEN,
    "expect_column_values_to_be_in_set": CHECK_IN_SET,
    "expect_column_values_to_not_be_in_set": CHECK_IN_SET,
    "expect_column_values_to_match_regex": CHECK_REGEX,
    "expect_column_values_to_not_match_regex": CHECK_REGEX,
    "expect_column_values_to_be_unique": CHECK_UNIQUE,
    "expect_column_values_to_be_of_type": CHECK_TYPE,
    "expect_column_values_to_be_in_type_list": CHECK_TYPE,
}

_CANONICAL_TO_GE: dict[str, str] = {
    CHECK_NOT_NULL: "expect_column_values_to_not_be_null",
    CHECK_UNIQUE: "expect_column_values_to_be_unique",
    CHECK_IN_SET: "expect_column_values_to_be_in_set",
    CHECK_BETWEEN: "expect_column_values_to_be_between",
    CHECK_REGEX: "expect_column_values_to_match_regex",
    CHECK_TYPE: "expect_column_values_to_be_of_type",
    CHECK_SQL: "expect_column_pair_values_to_be_equal",  # placeholder; discovery SQL uses custom path
}


@dataclass
class CanonicalRule:
    """Validator-neutral quality rule."""

    check: str
    column: Optional[str] = None
    params: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "column": self.column,
            "params": dict(self.params),
            "meta": dict(self.meta),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CanonicalRule":
        return cls(
            check=str(data.get("check") or ""),
            column=data.get("column"),
            params=dict(data.get("params") or {}),
            meta=dict(data.get("meta") or {}),
        )


def ge_rule_to_canonical(rule: dict[str, Any]) -> CanonicalRule:
    ge_name = str(rule.get("rule") or "")
    check = _GE_TO_CANONICAL.get(ge_name, ge_name)
    return CanonicalRule(
        check=check,
        column=rule.get("column"),
        params=dict(rule.get("kwargs") or {}),
        meta=dict(rule.get("meta") or {}),
    )


def canonical_to_ge_rule(rule: CanonicalRule) -> dict[str, Any]:
    ge_name = _CANONICAL_TO_GE.get(rule.check, rule.check)
    if not ge_name.startswith("expect_"):
        ge_name = f"expect_column_values_to_{rule.check}"
    out: dict[str, Any] = {
        "rule": ge_name,
        "column": rule.column,
        "kwargs": dict(rule.params),
    }
    if rule.meta:
        out["meta"] = dict(rule.meta)
    return out


def rule_set_to_canonical(rule_set: QualityRuleSet) -> list[CanonicalRule]:
    return [ge_rule_to_canonical(r) for r in (rule_set.rules or [])]


def canonical_to_rule_set(rules: list[CanonicalRule], *, name: Optional[str] = None) -> QualityRuleSet:
    return QualityRuleSet(
        name=name,
        rules=[canonical_to_ge_rule(r) for r in rules],
    )
