"""Tests for canonical quality rule vocabulary (POST_IMPLEMENTATION_REVIEW PR-D)."""

from __future__ import annotations

from redibis.quality.canonical_rules import (
    CHECK_NOT_NULL,
    CHECK_REGEX,
    CanonicalRule,
    canonical_to_ge_rule,
    canonical_to_rule_set,
    ge_rule_to_canonical,
    rule_set_to_canonical,
)
from redibis.quality.rule_set import QualityRuleSet


def test_ge_rule_round_trip_preserves_meta():
    original = {
        "rule": "expect_column_values_to_match_regex",
        "column": "notes",
        "kwargs": {"regex": r"[\u0600-\u06FF]", "mostly": 0.05},
        "meta": {"notes": {"content": "Arabic note"}},
    }
    canon = ge_rule_to_canonical(original)
    assert canon.check == CHECK_REGEX
    assert canon.meta["notes"]["content"] == "Arabic note"
    restored = canonical_to_ge_rule(canon)
    assert restored["rule"] == original["rule"]
    assert restored["kwargs"] == original["kwargs"]
    assert restored["meta"] == original["meta"]


def test_rule_set_canonical_round_trip():
    rs = QualityRuleSet(rules=[{
        "rule": "expect_column_values_to_not_be_null",
        "column": "id",
        "kwargs": {"mostly": 0.99},
    }])
    canonical = rule_set_to_canonical(rs)
    assert len(canonical) == 1
    assert canonical[0].check == CHECK_NOT_NULL
    back = canonical_to_rule_set(canonical)
    assert back.rules[0]["rule"] == "expect_column_values_to_not_be_null"
    assert back.rules[0]["column"] == "id"
