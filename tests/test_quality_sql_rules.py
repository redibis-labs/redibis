"""Custom SQL quality rules: one semantics in drafts, programs, parsing, monitoring and the UI probe."""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
import pytest

pytest.importorskip("great_expectations")
pytest.importorskip("duckdb")

from redibis.contracts.rule_code_parser import parse_ge_rules
from redibis.contracts.rules import _rule_to_ge, extract_rules
from redibis.quality.authoring import QualityDraft, draft_from_rules, relax_rules
from redibis.quality.contract_validate import quality_rules_from_contract, validate_contract_quality
from redibis.quality.sql_rules import (
    add_sql_results,
    check_query,
    normalize_sql_rule,
    run_sql_rules,
    sql_rule,
    sql_rule_to_contract,
)

TABLE = "shop.orders"


@pytest.fixture(autouse=True)
def _quiet():
    logging.getLogger("great_expectations").setLevel(logging.ERROR)


@pytest.fixture()
def orders() -> pd.DataFrame:
    return pd.DataFrame({
        "order_id": ["o1", "o2", "o3", "o4"],
        "amount": [10.0, 25.5, -3.0, 40.0],
        "currency": ["EGP", "USD", "EGP", "EUR"],
        "status": ["paid", "paid", "refunded", "paid"],
    })


def _one(df, rule, table=TABLE):
    return run_sql_rules(df, [rule], table=table)[0]


# ── semantics ────────────────────────────────────────────────────────────────

def test_row_query_counts_rows_that_break_the_rule(orders):
    row = _one(orders, sql_rule("SELECT * FROM ${object} WHERE amount < 0"))
    assert row["success"] is False and row["unexpected_count"] == 1
    assert row["partial_unexpected"][0]["order_id"] == "o3"
    assert "1 row(s) break the rule" in row["reason"]
    ok = _one(orders, sql_rule("SELECT * FROM ${object} WHERE amount < 0", max_failures=1))
    assert ok["success"] is True


def test_aggregate_query_is_compared_as_a_value(orders):
    count = _one(orders, sql_rule(
        "SELECT COUNT(*) FROM data WHERE currency NOT IN ('EGP', 'USD')"))
    assert count["observed_value"] == 1 and count["unexpected_count"] == 1 and not count["success"]
    assert _one(orders, sql_rule("SELECT COUNT(DISTINCT currency) FROM data", mustBe=3))["unexpected_count"] == 0
    avg = _one(orders, sql_rule("SELECT AVG(amount) FROM data", mustBeBetween=[0, 50]))
    assert avg["success"] and avg["observed_value"] == pytest.approx(18.125)
    cte = _one(orders, sql_rule(
        "WITH p AS (SELECT * FROM data WHERE status = 'paid') SELECT COUNT(*) FROM p",
        mustBe=3))
    assert cte["success"] and cte["observed_value"] == 3


def test_table_can_be_named_three_ways(orders):
    for q in ("SELECT * FROM ${object} WHERE amount < 0",
              "SELECT * FROM data WHERE amount < 0",
              "SELECT * FROM shop.orders WHERE amount < 0"):
        assert _one(orders, sql_rule(q))["unexpected_count"] == 1, q
    col = _one(orders, sql_rule("SELECT * FROM data WHERE ${property} IS NULL", column="status"))
    assert col["success"] and col["column"] == "status"


def test_only_one_select_is_accepted_and_files_are_off_limits(orders):
    with pytest.raises(ValueError, match="single statement"):
        check_query("SELECT 1; DROP TABLE data")
    with pytest.raises(ValueError, match="SELECT"):
        check_query("DELETE FROM data")
    assert check_query("select 1 -- ; comment\n;") == "select 1 -- ; comment"
    rule = {"rule": "sql", "sql": "SELECT * FROM read_csv('/etc/hostname')", "kwargs": {}}
    row = _one(orders, rule)
    assert row["success"] is False and row["observed_value"].startswith("error:")


def test_rule_shapes_normalize_to_one_form():
    ui = {"rule": "sql", "column": None, "sql": "SELECT * FROM data", "kwargs": {}}
    assert normalize_sql_rule(ui)["kwargs"] == {"sql": "SELECT * FROM data", "mustBeLessThan": 1}
    legacy = {"rule": "sql", "kwargs": {"query": "SELECT 1", "max_failures": 4}}
    assert normalize_sql_rule(legacy)["kwargs"] == {"sql": "SELECT 1", "mustBeLessThan": 5}
    assert sql_rule_to_contract(sql_rule("SELECT 1", description="d")) == {
        "type": "sql", "query": "SELECT 1", "mustBeLessThan": 1, "description": "d"}
    with pytest.raises(ValueError, match="unknown SQL rule threshold"):
        sql_rule("SELECT 1", mustBeAbout=3)


def test_report_totals_include_sql_results(orders):
    report = {"success": True, "statistics": {"evaluated_expectations": 1},
              "results": [{"rule": "expect_x", "success": True}]}
    merged = add_sql_results(report, orders, [sql_rule("SELECT * FROM data WHERE amount < 0")])
    assert merged["success"] is False
    assert merged["statistics"]["evaluated_expectations"] == 2
    assert merged["statistics"]["unsuccessful_expectations"] == 1


# ── drafts, contracts, monitoring ───────────────────────────────────────────

def test_draft_add_sql_validates_and_travels(orders):
    draft = draft_from_rules(TABLE, [
        {"rule": "expect_column_values_to_not_be_null", "column": "order_id", "kwargs": {}},
    ], df=orders)
    listed = draft.add_sql("SELECT * FROM ${object} WHERE amount < 0",
                           description="no negative amounts")
    assert listed["type"] == "sql" and listed["column"] is None
    assert listed["params"]["query"].startswith("SELECT * FROM ${object}")

    result = draft.validate(orders)
    sql = [r for r in result.results if r.expectation_type == "sql"]
    assert len(sql) == 1 and sql[0].success is False and result.status == "failed"

    rules = draft.to_rules()
    assert {"rule": "sql", "column": None,
            "kwargs": {"sql": "SELECT * FROM ${object} WHERE amount < 0", "mustBeLessThan": 1},
            "description": "no negative amounts"} in rules

    program = draft.to_program("pandas")
    assert "'rule': 'sql'" in program and "no negative amounts" in program
    back = QualityDraft.from_program(program, table=TABLE)
    assert [r["params"]["query"] for r in back.rules if r["type"] == "sql"] == [
        "SELECT * FROM ${object} WHERE amount < 0"]


def test_draft_from_rules_counts_sql_and_handles_sql_only(orders):
    rules = [
        {"rule": "expect_column_values_to_be_in_set", "column": "currency",
         "kwargs": {"value_set": ["EGP", "USD", "EUR"]}},
        sql_rule("SELECT * FROM data WHERE amount < 0"),
        sql_rule("SELECT COUNT(*) FROM data", mustBe=4),
    ]
    d = draft_from_rules(TABLE, rules, df=orders)
    assert (d.passed, d.total) == (2, 3)
    only_sql = draft_from_rules(TABLE, [sql_rule("SELECT * FROM data WHERE 1 = 0")], df=orders)
    assert (only_sql.passed, only_sql.total) == (1, 1)
    assert only_sql.payload["schema"][0]["quality"][0]["type"] == "sql"
    no_df = draft_from_rules(TABLE, [sql_rule("SELECT 1", mustBe=1)])
    assert [r["type"] for r in no_df.rules] == ["sql"]


def test_contract_sql_rules_run_in_monitoring(orders):
    contract = {"schema": [{"name": "orders", "quality": [
        {"type": "sql", "query": "SELECT COUNT(*) FROM shop.orders WHERE amount < 0",
         "mustBeLessThan": 1, "description": "no negative amounts"},
        {"type": "sql", "query": "SELECT * FROM ${object} WHERE status = 'void'"},
    ], "properties": [{"name": "order_id", "quality": [{"rule": "missingCount", "mustBe": 0}]}]}]}
    rs = quality_rules_from_contract(contract)
    assert sum(1 for r in rs.rules if r["rule"] == "sql") == 2
    result, _qa, _raw = validate_contract_quality(orders, contract, table=TABLE,
                                                  include_schema_drift=False)
    by_type = [(r.expectation_type, r.success) for r in result.results]
    assert ("sql", False) in by_type and ("sql", True) in by_type
    assert ("expect_column_values_to_not_be_null", True) in by_type
    assert result.rules_total == 3 and result.status == "failed"


def test_contract_keeps_every_sql_threshold():
    contract = {"schema": [{"name": "t", "quality": [
        {"type": "sql", "query": "SELECT AVG(x) FROM data", "mustBeBetween": [1, 2]}]}]}
    (rule,) = extract_rules(contract)
    assert rule.params == {"query": "SELECT AVG(x) FROM data", "mustBeBetween": [1, 2]}


def test_relax_leaves_sql_thresholds_alone():
    payload = {"schema": [{"name": "t", "quality": [
        {"type": "sql", "query": "SELECT AVG(x) FROM data", "mustBeBetween": [1, 2]},
        {"rule": "rowCount", "mustBeBetween": [100, 200]}]}]}
    relaxed, changed = relax_rules(payload, 0.5)
    assert changed == 1
    assert relaxed["schema"][0]["quality"][0]["mustBeBetween"] == [1, 2]


def test_be_null_round_trips_as_be_null():
    d = draft_from_rules(TABLE, [
        {"rule": "expect_column_values_to_be_null", "column": "legacy", "kwargs": {}}])
    (rule,) = extract_rules(d.payload)
    assert _rule_to_ge(rule)["expectation_type"] == "expect_column_values_to_be_null"


# ── generated program + safe parser ─────────────────────────────────────────

def test_parser_reads_sql_rules_in_every_program_form():
    code = '''
RULES: list[dict] = [
    {"rule": "expect_column_values_to_not_be_null", "column": "id", "kwargs": {}},
    {"rule": "sql", "column": None, "kwargs": {"sql": "SELECT * FROM data WHERE a < 0", "mustBeLessThan": 1}},
    sql_rule("SELECT COUNT(*) FROM ${object} WHERE b IS NULL", max_failures=2, description="few nulls"),
]
RULES.append(sql_rule("SELECT * FROM data WHERE c = ''"))
RULES.append({"rule": "sql", "sql": "SELECT * FROM data WHERE d = 0"})
RULES.append(sql_rule(__import__("os").getcwd()))
'''
    parsed = parse_ge_rules(code)
    sql = [r for r in parsed["rules"] if r["expectation_type"] == "sql"]
    assert [r["kwargs"]["sql"] for r in sql] == [
        "SELECT * FROM data WHERE a < 0",
        "SELECT COUNT(*) FROM ${object} WHERE b IS NULL",
        "SELECT * FROM data WHERE c = ''",
        "SELECT * FROM data WHERE d = 0",
    ]
    assert sql[1]["kwargs"]["mustBeLessThan"] == 3 and sql[1]["description"] == "few nulls"
    assert any("literal" in e["message"] for e in parsed["errors"])


def test_generated_program_runs_sql_rules_and_dedupes_by_query(orders, tmp_path):
    d = draft_from_rules(TABLE, [
        {"rule": "expect_column_values_to_not_be_null", "column": "order_id", "kwargs": {}}])
    ns = {"__name__": "__main__", "get_ipython": lambda: object()}
    exec(compile(d.to_program("pandas"), "prog.py", "exec"), ns)
    ns["add_rules"]([ns["sql_rule"]("SELECT * FROM data WHERE amount < 0", max_failures=1),
                     ns["sql_rule"]("SELECT * FROM data WHERE status = 'void'")])
    assert sum(1 for r in ns["RULES"] if r["rule"] == "sql") == 2
    report = ns["validate"](orders)
    sql = [r for r in report["results"] if r["rule"] == "sql"]
    assert [r["success"] for r in sql] == [True, True] and report["success"] is True
    run_id = ns["save_quality_run"](orders, output_dir=str(tmp_path))
    from redibis.quality.authoring import QualityAuthor

    saved = QualityAuthor(TABLE, output_dir=str(tmp_path)).load(run_id)
    assert sum(1 for r in saved.rules if r["type"] == "sql") == 2


def test_quality_page_probe_uses_the_same_semantics(orders):
    from redibis.services.discovery_service import DiscoveryService, QualityProbe

    svc = DiscoveryService()
    count = svc.probe_quality(QualityProbe(
        rule="sql", sql="SELECT COUNT(*) FROM data WHERE amount < -100"), orders)
    assert count.success is True and count.unexpected_count == 0
    rows = svc.probe_quality(QualityProbe(
        rule="sql", sql="SELECT * FROM data WHERE amount < 0"), orders)
    assert rows.success is False and rows.unexpected_count == 1


def test_sql_rules_on_spark(orders):
    pytest.importorskip("pyspark")
    from pyspark.sql import SparkSession

    spark = (SparkSession.builder.master("local[1]").appName("redibis-sql-rules-test")
             .config("spark.ui.enabled", "false").getOrCreate())
    try:
        sdf = spark.createDataFrame(orders)
        sdf.createOrReplaceTempView("data")   # the user's own view must survive
        rows = run_sql_rules(sdf, [
            sql_rule("SELECT * FROM data WHERE amount < 0"),
            sql_rule("SELECT COUNT(*) FROM shop.orders WHERE currency = 'EUR'", mustBe=1),
            sql_rule("SELECT * FROM ${object} WHERE amount < 0", max_failures=1),
        ], table=TABLE)
        assert [r["success"] for r in rows] == [False, True, True]
        assert rows[0]["unexpected_count"] == 1
        assert spark.catalog.tableExists("data")
        assert not [t for t in spark.catalog.listTables() if t.name.startswith("redibis_sql_")]
    finally:
        spark.stop()


def test_paste_fragment_keeps_sql_rules_parseable():
    from redibis.quality.ge_codegen import render_ge_paste_module

    text = render_ge_paste_module([
        {"rule": "expect_column_values_to_not_be_null", "column": "id", "kwargs": {}},
        sql_rule("SELECT * FROM ${object} WHERE amount < 0", description="no negatives"),
        sql_rule("SELECT COUNT(*) FROM data WHERE ${property} = ''", column="id", max_failures=2),
    ])
    assert "sql_rule(" in text and "expectation_name='sql'" not in text
    parsed = parse_ge_rules(text)
    assert not parsed["errors"]
    sql = [r for r in parsed["rules"] if r["expectation_type"] == "sql"]
    assert [(r["column"], r["kwargs"]["mustBeLessThan"]) for r in sql] == [(None, 1), ("id", 3)]
    assert sql[0]["description"] == "no negatives"
