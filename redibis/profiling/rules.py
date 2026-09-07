"""
Map GE expectation objects to the neutral ``QualityRuleSet`` vocabulary.
"""

from __future__ import annotations

from typing import Any, Iterable

from redibis.quality.rule_set import QualityRuleSet


def expectations_to_rule_set(expectations: Iterable[Any]) -> QualityRuleSet:
    """Convert GE ``ExpectationConfiguration`` objects → ``QualityRuleSet``."""
    rules: list[dict] = []
    for exp in expectations or []:
        etype = getattr(exp, "expectation_type", None) or getattr(exp, "type", None)
        if not etype:
            continue
        kwargs = dict(getattr(exp, "kwargs", {}) or {})
        column = kwargs.pop("column", None)
        entry: dict[str, Any] = {
            "rule": etype,
            "column": column,
            "kwargs": kwargs,
        }
        meta = getattr(exp, "meta", None) or {}
        if meta:
            entry["meta"] = dict(meta)
        rules.append(entry)
    return QualityRuleSet(rules=rules)
