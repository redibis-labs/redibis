"""
redibis.quality.rule_set
=========================
QualityRuleSet — the user-editable quality-rules list, embedded directly in
the global config.

This is the quality-domain twin of ``redibis.pii.regex_overrides.RegexSet``:
a first-class, named, persistable list of quality rules that the discovery /
settings pages mutate (add / remove / modify a rule) and then re-run a scan
against.

Each rule is a plain dict so the list round-trips cleanly to YAML/JSON and can
be fed straight into ``QualityGatekeeper.add_gx_expectation`` (or evaluated by
the discovery probe).  Canonical rule shape::

    {
        "rule":   "expect_column_values_to_not_be_null",  # GE expectation name or short alias
        "column": "phone",                                # optional (table-level rules omit it)
        "kwargs": {"mostly": 0.95},                       # rule-specific params
        "meta":   {"notes": {"content": "..."}},          # optional; GE-only, ignored by other validators
    }
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class QualityRuleSet:
    """Editable list of quality rules carried inside the global config."""

    name: Optional[str] = None
    rules: list[dict] = field(default_factory=list)

    # ── Mutation helpers (used by the discovery / settings layer) ──────────
    def add_rule(
        self,
        rule: str,
        column: Optional[str] = None,
        kwargs: Optional[dict] = None,
    ) -> "QualityRuleSet":
        self.rules.append(
            {"rule": rule, "column": column, "kwargs": dict(kwargs or {})}
        )
        return self

    def remove_rule(self, index: int) -> bool:
        if 0 <= index < len(self.rules):
            del self.rules[index]
            return True
        return False

    def remove_where(self, *, rule: Optional[str] = None, column: Optional[str] = None) -> int:
        """Remove every rule matching the given rule name and/or column. Returns count removed."""
        before = len(self.rules)
        self.rules = [
            r for r in self.rules
            if not (
                (rule is None or r.get("rule") == rule)
                and (column is None or r.get("column") == column)
            )
        ]
        return before - len(self.rules)

    def clear(self) -> "QualityRuleSet":
        self.rules = []
        return self

    # ── Serialization ──────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {"name": self.name, "rules": list(self.rules)}

    @classmethod
    def from_dict(cls, data: Any) -> "QualityRuleSet":
        if not data:
            return cls()
        # Accept the full {name, rules} shape OR a bare list of rules.
        if isinstance(data, list):
            return cls(rules=list(data))
        return cls(name=data.get("name"), rules=list(data.get("rules", [])))

    def __bool__(self) -> bool:
        return bool(self.rules)

    def __len__(self) -> int:
        return len(self.rules)
