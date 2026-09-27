"""Validation that pipelines can gate on: severities, samples, missing columns, engine
errors — plus the authoring helpers that set them (set_severity, add_rules)."""

from __future__ import annotations

import logging

import pandas as pd
import pytest

pytest.importorskip("great_expectations")

from redibis.quality.authoring import (
    QualityDraft,
    _canonicalize,
    draft_from_rules,
    drop_rules,
    set_severity,
)
from redibis.quality.contract_validate import validate_contract_quality
from redibis.quality.sql_rules import sql_rule

TABLE = "shop.accounts"


@pytest.fixture(autouse=True)
def _quiet():
    logging.getLogger("great_expectations").setLevel(logging.ERROR)


@pytest.fixture()
def accounts() -> pd.DataFrame:
    return pd.DataFrame({
        "id": ["a1", "a2", "a3", None],
        "amount": [10.0, 20.0, -5.0, 7.0],
        "country": ["EG", "SA", "XX", "EG"],
        "profile": ['{"lang": "ar"}', '{"lang": "en"}', '{"lang": "ar"', '{"lang": "en"}'],
    })


def _draft(*rules) -> QualityDraft:
    return draft_from_rules(TABLE, list(rules))


def R(expectation, column=None, **kwargs):
    return {"rule": expectation, "column": column, "kwargs": kwargs}


def _by(result, etype, column):
    return next(r for r in result.results if r.expectation_type == etype and r.column == column)


# ── severities and samples reach the results ──────────────────────────────────

def test_contract_severity_and_rule_id_reach_the_results(accounts):
    d = _draft(R("expect_column_values_to_not_be_null", "id"),
               R("expect_column_values_to_be_between", "amount", min_value=0, max_value=100))
    d.set_severity("P2", columns=["amount"])
    listed = {r["column"]: r for r in d.rules}
    result, _qa, _raw = validate_contract_quality(accounts, d.payload, table=TABLE)
    amount = _by(result, "expect_column_values_to_be_between", "amount")
    assert amount.severity == "P2" and amount.rule_id == listed["amount"]["id"]
    assert _by(result, "expect_column_values_to_not_be_null", "id").severity == "P1"


def test_failing_rules_carry_sample_values(accounts):
    d = _draft(R("expect_column_values_to_be_in_set", "country", value_set=["EG", "SA"]))
    result, _qa, _raw = validate_contract_quality(accounts, d.payload, table=TABLE)
    assert result.results[0].partial_unexpected == ["XX"]


# ── a missing column or an engine error does not hide the other results ───────

def test_rules_on_missing_columns_report_and_the_rest_runs(accounts):
    d = _draft(R("expect_column_values_to_not_be_null", "id"),
               R("expect_column_values_to_not_be_null", "gone"),
               R("expect_column_pair_values_to_be_equal", None, column_A="id", column_B="gone_too"),
               R("expect_table_columns_to_match_set", None,
                 column_set=["id", "amount", "country", "profile", "gone"]))
    result, _qa, _raw = validate_contract_quality(accounts, d.payload, table=TABLE)
    missing = [r for r in result.results if r.message.startswith("column missing")]
    assert {r.column for r in missing} == {"gone", None}
    assert "gone_too" in next(r.message for r in missing if r.column is None)
    assert _by(result, "expect_column_values_to_not_be_null", "id").unexpected_count == 1
    shape = _by(result, "expect_table_columns_to_match_set", None)
    assert not shape.success and not shape.message.startswith("column missing")


def test_a_rule_that_raises_is_isolated(accounts):
    d = _draft(R("expect_column_values_to_match_json_schema", "profile",
                 json_schema={"type": "object", "required": ["lang"]}),
               R("expect_column_values_to_not_be_null", "id"))
    result, _qa, _raw = validate_contract_quality(accounts, d.payload, table=TABLE)
    schema = _by(result, "expect_column_values_to_match_json_schema", "profile")
    assert not schema.success and schema.message.startswith("could not run")
    not_null = _by(result, "expect_column_values_to_not_be_null", "id")
    assert not_null.unexpected_count == 1 and not not_null.message


def test_spark_bundle_failure_is_isolated(accounts):
    pytest.importorskip("pyspark")
    from pyspark.sql import SparkSession

    spark = (SparkSession.builder.master("local[1]").appName("redibis-robustness-test")
             .config("spark.ui.enabled", "false").getOrCreate())
    try:
        d = _draft(R("expect_column_values_to_match_json_schema", "profile",
                     json_schema={"type": "object"}),
                   R("expect_column_values_to_not_be_null", "id"),
                   R("expect_column_values_to_be_in_set", "country", value_set=["EG", "SA"]))
        result, _qa, _raw = validate_contract_quality(spark.createDataFrame(accounts), d.payload,
                                                      table=TABLE)
        assert _by(result, "expect_column_values_to_match_json_schema", "profile").message.startswith(
            "could not run")
        assert _by(result, "expect_column_values_to_not_be_null", "id").unexpected_count == 1
        assert _by(result, "expect_column_values_to_be_in_set", "country").partial_unexpected == ["XX"]
    finally:
        spark.stop()


# ── authoring helpers ────────────────────────────────────────────────────────

def test_set_severity_combines_filters_and_selects_table_level():
    d = _draft(R("expect_column_values_to_be_in_set", "country", value_set=["EG"]),
               R("expect_column_values_to_not_be_null", "country"),
               R("expect_column_values_to_not_be_null", "id"),
               R("expect_table_row_count_to_be_between", None, min_value=1, max_value=9))
    changed = d.set_severity("P2", columns=["country"], rule_types=["expect_column_values_to_be_in_set"])
    assert [(r["column"], r["type"]) for r in changed] == [("country", "set")]
    d.set_severity("P3", columns=[None])
    by = {(r["column"], r["type"]): r["severity"] for r in d.rules}
    assert by == {("country", "set"): "P2", ("country", "not_null"): "P1",
                  ("id", "not_null"): "P1", (None, "row_count"): "P3"}
    with pytest.raises(ValueError, match="severity"):
        d.set_severity("urgent")
    assert set_severity(d.payload, "P1")[1]   # the functional form returns the changed rules


def test_drop_matches_great_expectations_names_of_portable_rules():
    d = _draft(R("expect_column_values_to_be_in_set", "country", value_set=["EG"]),
               R("expect_column_values_to_not_be_null", "id"))
    _payload, removed = drop_rules(d.payload, rule_types=["expect_column_values_to_be_in_set"])
    assert [r["type"] for r in removed] == ["set"]


def test_add_rules_and_add_sql_extend_a_draft(accounts):
    d = _draft(R("expect_column_values_to_not_be_null", "id"))
    added = d.add_rules([R("expect_column_values_to_be_between", "amount", min_value=0),
                         R("expect_compound_columns_to_be_unique", None, column_list=["id", "country"]),
                         sql_rule("SELECT * FROM ${object} WHERE country = 'XX'")])
    assert sorted((str(r["column"]), r["type"]) for r in added) == [
        ("None", "engine"), ("None", "sql"), ("amount", "engine")]
    assert d.add_rules([R("expect_column_values_to_be_between", "amount", min_value=0)]) == []
    listed = d.add_sql("SELECT COUNT(*) FROM ${object} WHERE amount < 0", severity="P2")
    assert listed["severity"] == "P2"
    with pytest.raises(ValueError, match="severity"):
        d.add_sql("SELECT 1", severity="high")
    result = d.validate(accounts)
    assert result.rules_total == len(d.rules)


def test_canonical_kwargs_order_is_stable():
    payload = {"schema": [{"name": "t", "properties": [{"name": "x", "quality": [
        {"engine": "greatExpectations", "implementation": {
            "expectation_type": "expect_column_values_to_be_between",
            "kwargs": {"strict_max": False, "column": "x", "max_value": 2, "min_value": 1}}}]}]}]}
    kwargs = _canonicalize(payload)["schema"][0]["properties"][0]["quality"][0]["implementation"]["kwargs"]
    assert list(kwargs) == ["column", "max_value", "min_value", "strict_max"]
