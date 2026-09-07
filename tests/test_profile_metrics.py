"""Unit tests for ``redibis.scan.profile_metrics`` (evidence numeric group).

``parsed_count`` is the histogram denominator. ``counts.non_null`` is not —
unparsed strings sit in non_null but never enter a bin. Without ``parsed_count``
that gap is unassertable.
"""

from __future__ import annotations

import redibis.scan.profile_metrics as pm


def test_parsed_count_is_histogram_denominator():
    values = list(range(100)) + [None, None]
    numeric = pm.profile_numeric(values, bins=10)
    counts = pm.profile_counts(values)
    assert numeric is not None
    assert numeric["parsed_count"] == 100
    assert numeric["parsed_count"] == counts["non_null"]
    assert sum(numeric["histogram"]["counts"]) == numeric["parsed_count"]


def test_parsed_count_less_than_non_null_when_some_values_fail_parse():
    # 60 numbers + 40 unparseable strings → parse rate 0.60 ≥ min 0.50.
    values = list(range(60)) + ["n/a"] * 40
    numeric = pm.profile_numeric(values, bins=10)
    counts = pm.profile_counts(values)
    assert numeric is not None
    assert counts["non_null"] == 100
    assert numeric["parsed_count"] == 60
    assert numeric["parsed_count"] < counts["non_null"]
    assert sum(numeric["histogram"]["counts"]) == numeric["parsed_count"]


def test_zero_and_negative_counts_are_subsets_of_parsed():
    values = [-3, -1, 0, 0, 2, 5, "foo", None]
    numeric = pm.profile_numeric(values, bins=5, min_parse_rate=0.5)
    assert numeric is not None
    assert numeric["parsed_count"] == 6
    assert numeric["zero_count"] == 2
    assert numeric["negative_count"] == 2
    assert numeric["zero_count"] + numeric["negative_count"] <= numeric["parsed_count"]
    assert sum(numeric["histogram"]["counts"]) == numeric["parsed_count"]


def test_numeric_none_below_min_parse_rate():
    values = [1, 2, "x", "y", "z", "w"]
    assert pm.profile_numeric(values, min_parse_rate=0.5) is None
