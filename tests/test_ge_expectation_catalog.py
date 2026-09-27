"""Every Great Expectations core expectation: runs through redibis, survives the contract.

docs/QUALITY_RULES.md lists the full catalogue with per-engine support. This test
keeps that list honest: it fails when the installed GE adds or drops an
expectation, when one stops round-tripping through the contract, or when the
pandas support changes.
"""

from __future__ import annotations

import json
import logging
import warnings
from pathlib import Path

import pandas as pd
import pytest

pytest.importorskip("great_expectations")

from redibis.quality.authoring import draft_from_rules
from redibis.quality.gatekeeper import QualityGatekeeper
from redibis.quality.rule_set import QualityRuleSet
from redibis.services.pipeline import apply_quality_rules

DOC = Path(__file__).resolve().parents[1] / "docs" / "QUALITY_RULES.md"

# GE 0.17 has no pandas implementation for these (SQL engines only), or the
# check misreads pandas text columns (in_type_list). Documented in the catalogue.
PANDAS_UNSUPPORTED = {
    "expect_column_values_to_be_in_type_list",
    "expect_column_values_to_match_like_pattern",
    "expect_column_values_to_match_like_pattern_list",
    "expect_column_values_to_not_match_like_pattern",
    "expect_column_values_to_not_match_like_pattern_list",
    "expect_table_row_count_to_equal_other_table",
}

pdf = pd.DataFrame({
    "id": [1, 2, 3, 4, 5, 6],
    "amount": [10.0, 20.0, 30.0, 40.0, 50.0, 60.0],
    "fee": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
    "total": [11.0, 22.0, 33.0, 44.0, 55.0, 66.0],
    "currency": ["EGP", "EGP", "USD", "USD", "EUR", "EGP"],
    "code": ["AB1", "AB2", "AB3", "AB4", "AB5", "AB6"],
    "email": ["a@x.io", "b@x.io", "c@x.io", "d@x.io", "e@x.io", "f@x.io"],
    "day": ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-05", "2026-09-06"],
    "js": ['{"a": 1}'] * 6,
    "empty": [None] * 6,
})
C = ["id", "amount", "fee", "total", "currency", "code", "email", "day", "js", "empty"]
K = {
 "expect_column_distinct_values_to_be_in_set": ("currency", {"value_set": ["EGP", "USD", "EUR"]}),
 "expect_column_distinct_values_to_contain_set": ("currency", {"value_set": ["EGP", "USD"]}),
 "expect_column_distinct_values_to_equal_set": ("currency", {"value_set": ["EGP", "USD", "EUR"]}),
 "expect_column_kl_divergence_to_be_less_than": ("currency", {"partition_object": {"values": ["EGP", "USD", "EUR"], "weights": [0.5, 0.3333, 0.1667]}, "threshold": 0.1}),
 "expect_column_max_to_be_between": ("amount", {"min_value": 50, "max_value": 70}),
 "expect_column_mean_to_be_between": ("amount", {"min_value": 30, "max_value": 40}),
 "expect_column_median_to_be_between": ("amount", {"min_value": 30, "max_value": 40}),
 "expect_column_min_to_be_between": ("amount", {"min_value": 0, "max_value": 10}),
 "expect_column_most_common_value_to_be_in_set": ("currency", {"value_set": ["EGP"]}),
 "expect_column_pair_values_a_to_be_greater_than_b": (None, {"column_A": "total", "column_B": "amount"}),
 "expect_column_pair_values_to_be_equal": (None, {"column_A": "fee", "column_B": "fee"}),
 "expect_column_pair_values_to_be_in_set": (None, {"column_A": "currency", "column_B": "code", "value_pairs_set": [[c, k] for c, k in zip(pdf.currency, pdf.code)]}),
 "expect_column_proportion_of_unique_values_to_be_between": ("id", {"min_value": 0.9, "max_value": 1.0}),
 "expect_column_quantile_values_to_be_between": ("amount", {"quantile_ranges": {"quantiles": [0.5], "value_ranges": [[20, 40]]}}),
 "expect_column_stdev_to_be_between": ("amount", {"min_value": 10, "max_value": 30}),
 "expect_column_sum_to_be_between": ("amount", {"min_value": 200, "max_value": 220}),
 "expect_column_to_exist": ("amount", {}),
 "expect_column_unique_value_count_to_be_between": ("currency", {"min_value": 3, "max_value": 3}),
 "expect_column_value_lengths_to_be_between": ("code", {"min_value": 3, "max_value": 3}),
 "expect_column_value_lengths_to_equal": ("code", {"value": 3}),
 "expect_column_value_z_scores_to_be_less_than": ("amount", {"threshold": 3, "double_sided": True}),
 "expect_column_values_to_be_between": ("amount", {"min_value": 0, "max_value": 100}),
 "expect_column_values_to_be_dateutil_parseable": ("day", {}),
 "expect_column_values_to_be_decreasing": ("amount", {"mostly": 0.0}),
 "expect_column_values_to_be_in_set": ("currency", {"value_set": ["EGP", "USD", "EUR"]}),
 "expect_column_values_to_be_in_type_list": ("currency", {"type_list": ["str", "StringType"]}),
 "expect_column_values_to_be_increasing": ("id", {}),
 "expect_column_values_to_be_json_parseable": ("js", {}),
 "expect_column_values_to_be_null": ("empty", {}),
 "expect_column_values_to_be_of_type": ("currency", {"type_": "__ENGINE_STR__"}),
 "expect_column_values_to_be_unique": ("id", {}),
 "expect_column_values_to_match_json_schema": ("js", {"json_schema": {"type": "object"}}),
 "expect_column_values_to_match_like_pattern": ("code", {"like_pattern": "AB%"}),
 "expect_column_values_to_match_like_pattern_list": ("code", {"like_pattern_list": ["AB%"]}),
 "expect_column_values_to_match_regex": ("code", {"regex": "^AB[0-9]$"}),
 "expect_column_values_to_match_regex_list": ("code", {"regex_list": ["^AB", "[0-9]$"], "match_on": "any"}),
 "expect_column_values_to_match_strftime_format": ("day", {"strftime_format": "%Y-%m-%d"}),
 "expect_column_values_to_not_be_in_set": ("currency", {"value_set": ["GBP"]}),
 "expect_column_values_to_not_be_null": ("id", {}),
 "expect_column_values_to_not_match_like_pattern": ("code", {"like_pattern": "ZZ%"}),
 "expect_column_values_to_not_match_like_pattern_list": ("code", {"like_pattern_list": ["ZZ%"]}),
 "expect_column_values_to_not_match_regex": ("code", {"regex": "^ZZ"}),
 "expect_column_values_to_not_match_regex_list": ("code", {"regex_list": ["^ZZ", "^YY"]}),
 "expect_compound_columns_to_be_unique": (None, {"column_list": ["id", "currency"]}),
 "expect_multicolumn_sum_to_equal": (None, {"column_list": ["amount", "fee"], "sum_total": 0, "mostly": 0.0}),
 "expect_select_column_values_to_be_unique_within_record": (None, {"column_list": ["amount", "fee"]}),
 "expect_table_column_count_to_be_between": (None, {"min_value": 10, "max_value": 10}),
 "expect_table_column_count_to_equal": (None, {"value": 10}),
 "expect_table_columns_to_match_ordered_list": (None, {"column_list": C}),
 "expect_table_columns_to_match_set": (None, {"column_set": C}),
 "expect_table_row_count_to_be_between": (None, {"min_value": 6, "max_value": 6}),
 "expect_table_row_count_to_equal": (None, {"value": 6}),
 "expect_table_row_count_to_equal_other_table": (None, {"other_table_name": "other"}),
}



def _registered() -> set[str]:
    import great_expectations.expectations.core  # noqa: F401 — registers the core set
    from great_expectations.expectations.registry import _registered_expectations

    return {name for name, cls in _registered_expectations.items()
            if cls.__module__.startswith("great_expectations.expectations.core")}


def test_catalogue_covers_every_registered_expectation():
    assert _registered() == set(K)


def test_docs_list_every_expectation():
    text = DOC.read_text(encoding="utf-8")
    missing = sorted(e for e in K if f"`{e}`" not in text)
    assert not missing, f"add to docs/QUALITY_RULES.md: {missing}"


@pytest.mark.parametrize("etype", sorted(K))
def test_contract_round_trip(etype):
    col, kw = K[etype]
    draft = draft_from_rules("t.chk", [{"rule": etype, "column": col, "kwargs": kw}])
    assert etype in {r["rule"] for r in draft.to_rules()}


def test_pandas_support_matches_the_docs():
    logging.getLogger("great_expectations").setLevel(logging.ERROR)
    warnings.filterwarnings("ignore")
    passed = set()
    for etype, (col, kw) in K.items():
        kw = json.loads(json.dumps(kw))
        if kw.get("type_") == "__ENGINE_STR__":
            kw["type_"] = "str"
        qa = QualityGatekeeper(suite_name="chk", in_memory=True)
        qa.attach_dataframe(pdf, dataset_name="chk")
        apply_quality_rules(qa, QualityRuleSet(name="chk", rules=[
            {"rule": etype, "column": col, "kwargs": kw}]), profiler_expectations=[])
        rows = qa._extract_report_data(qa.run_tests(stage="chk", generate_docs=False))["results"]
        if rows and all(r["success"] for r in rows):
            passed.add(etype)
    assert set(K) - passed == PANDAS_UNSUPPORTED
