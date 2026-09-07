"""Translate OpenMetadata-style metrics → neutral ``QualityRuleSet`` (GE vocabulary)."""

from __future__ import annotations

from typing import Any

from redibis.profiling.om_metrics import ColumnMetric
from redibis.quality.rule_set import QualityRuleSet


def _round_mostly(value: float) -> float:
    return max(0.0, min(1.0, round(value, 4)))


def metrics_to_rule_set(
    metrics: dict[str, ColumnMetric],
    *,
    max_frequent_values: int = 10,
) -> QualityRuleSet:
    """
    Suggest GE expectations from column metrics.

    Examples:
      - low null rate → ``expect_column_values_to_not_be_null``
      - high unique ratio → ``expect_column_values_to_be_unique``
      - numeric min/max → ``expect_column_values_to_be_between``
      - small stable set → ``expect_column_values_to_be_in_set``
    """
    rules: list[dict[str, Any]] = []

    for col, m in metrics.items():
        if m.null_proportion < 0.05 and m.row_count:
            mostly = _round_mostly(1.0 - m.null_proportion)
            if mostly >= 0.5:
                rules.append({
                    "rule": "expect_column_values_to_not_be_null",
                    "column": col,
                    "kwargs": {"mostly": mostly},
                })

        if m.unique_proportion >= 0.98 and m.unique_count > 1:
            rules.append({
                "rule": "expect_column_values_to_be_unique",
                "column": col,
                "kwargs": {},
            })

        if m.min_value is not None and m.max_value is not None:
            try:
                min_v = float(m.min_value)
                max_v = float(m.max_value)
                if min_v <= max_v:
                    rules.append({
                        "rule": "expect_column_values_to_be_between",
                        "column": col,
                        "kwargs": {"min_value": min_v, "max_value": max_v},
                    })
            except (TypeError, ValueError):
                pass

        if 1 < m.unique_count <= max_frequent_values and m.frequent_values:
            value_set = [v for v, _ in m.frequent_values]
            # Only when top values cover every distinct value (not truncated top-N).
            if len(value_set) >= 2 and m.unique_count <= len(value_set):
                rules.append({
                    "rule": "expect_column_values_to_be_in_set",
                    "column": col,
                    "kwargs": {"value_set": value_set},
                })

    return QualityRuleSet(rules=rules)
