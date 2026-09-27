"""Writing rules in a few lines: QualityDraft.new / add_gx_expectation / expect_… / scan / remove."""

from __future__ import annotations

import copy
import logging
import pickle

import pandas as pd
import pytest

pytest.importorskip("great_expectations")

from redibis.quality import QualityDraft, merge, scan
from redibis.quality.authoring import ADHOC_TABLE, SNAPSHOT_RULES
from redibis.quality.ge_codegen import render_gx_expectation_line


@pytest.fixture(autouse=True, scope="module")
def _quiet_ge():
    logging.getLogger("great_expectations").setLevel(logging.ERROR)


def _frame(n: int = 200) -> pd.DataFrame:
    return pd.DataFrame({
        "id": [f"C{i:05d}" for i in range(n)],
        "country": ["EG", "SA", "AE", "EG"] * (n // 4),
        "points": [i * 10 for i in range(n)],
    })


def _bad() -> pd.DataFrame:
    df = _frame()
    df.loc[:9, "id"] = "C00000"          # 10 duplicates
    df.loc[:4, "country"] = "XX"
    df.loc[:2, "points"] = -5
    return df


def test_quick_rule_on_any_dataframe_needs_no_table():
    qa = QualityDraft.new()
    assert qa.table == ADHOC_TABLE and qa.rules == []
    result = qa.expect_column_values_to_be_unique("id").validate(_bad())
    frame = result.to_frame()
    assert list(frame.expectation) == ["expect_column_values_to_be_unique"]
    assert not frame.passed.iloc[0] and frame.unexpected.iloc[0] > 0


def test_the_quality_page_line_pastes_verbatim_with_severity():
    qa = QualityDraft.new("shop.customers")
    line = render_gx_expectation_line("expect_column_values_to_not_be_null", column="id", kwargs={})
    exec(line, {"qa": qa})                                             # the generated line, as is
    qa.add_gx_expectation(expectation_name="expect_column_values_to_be_in_set", column="country",
                          value_set=["EG", "SA", "AE"], severity="P2", meta={"notes": "ignored"})
    rules = qa.rules
    assert [r["column"] for r in rules] == ["id", "country"]
    assert rules[1]["severity"] == "P2"
    assert "meta" not in rules[1]["params"]
    assert qa.validate(_frame()).status == "success"


def test_expect_calls_chain_and_mix_with_sql():
    qa = (QualityDraft.new("shop.t")
          .expect_column_values_to_be_between("points", min_value=0, severity="P1")
          .expect_compound_columns_to_be_unique(column_list=["id", "country"]))
    qa.add_sql("SELECT * FROM ${object} WHERE country = 'XX'", description="no test country")
    frame = qa.validate(_bad()).to_frame()
    assert set(frame.expectation) == {"expect_column_values_to_be_between",
                                      "expect_compound_columns_to_be_unique", "sql"}
    assert not frame.passed.any()
    assert frame.set_index("expectation").loc["expect_column_values_to_be_between", "severity"] == "P1"
    failed = qa.validate(_frame()).to_frame(only_failed=True)
    assert failed.empty


def test_misspelt_expectations_fail_early_with_a_suggestion():
    qa = QualityDraft.new()
    with pytest.raises(ValueError, match="did you mean 'expect_column_values_to_be_unique'"):
        qa.expect_column_value_to_be_unique("id")
    with pytest.raises(ValueError, match="severity"):
        qa.expect_column_values_to_be_unique("id", severity="high")
    with pytest.raises(AttributeError):
        qa.not_an_expectation                                          # noqa: B018
    assert qa.rules == []


def test_drafts_with_dynamic_expectations_still_copy_and_pickle():
    qa = QualityDraft.new("shop.t").expect_column_values_to_not_be_null("id")
    assert copy.deepcopy(qa).rules == qa.rules
    assert pickle.loads(pickle.dumps(qa)).rules == qa.rules


def test_scan_discovers_curated_rules_ready_to_edit():
    df = _frame()
    qa = scan(df, "shop.customers")
    assert qa.table == "shop.customers" and qa.rules
    names = {n for r in qa.rules for n in (r["type"], r["expectation_type"])}
    assert not names & set(SNAPSHOT_RULES)                               # no one-sample rules
    assert qa.total == 0                                                 # edited: not validated yet

    kept = scan(df, keep_snapshot_rules=True, relax=0)
    assert kept.table == ADHOC_TABLE and len(kept.rules) > len(qa.rules)
    assert kept.total > 0

    before = len(qa.rules)
    qa.remove("not_null", column="id")
    assert len(qa.rules) == before - 1
    assert not [r for r in qa.rules if r["column"] == "id" and r["type"] == "not_null"]
    assert [r for r in qa.rules if r["column"] == "id"], "other rules on id stay"

    qa.remove(column="country")
    assert not [r for r in qa.rules if r["column"] == "country"]
    qa.expect_column_values_to_be_between("points", min_value=0, severity="P1")
    assert qa.validate(_bad()).to_frame(only_failed=True).column.eq("points").any()


def test_remove_needs_a_filter_and_reports_no_match():
    qa = QualityDraft.new().expect_column_values_to_be_unique("id")
    with pytest.raises(ValueError, match="needs"):
        qa.remove()
    with pytest.raises(ValueError, match="no rule matches"):
        qa.remove("expect_column_values_to_be_unique", column="country")
    assert qa.remove(index=0).rules == []


def test_to_frame_and_str_summarise_a_validation():
    result = QualityDraft.new("shop.t").expect_column_values_to_be_unique("id").validate(_bad())
    assert "0/1 rules passed" in str(result)
    frame = result.to_frame()
    assert list(frame.columns) == ["expectation", "column", "severity", "passed", "observed",
                                   "unexpected", "sample", "message"]
    assert len(frame["sample"].iloc[0]) <= 5


def test_to_code_is_readable_and_runs_back_to_the_same_rules():
    qa = scan(_frame(), "shop.customers")
    qa.set_severity("P2", columns=["country"])
    qa.expect_column_values_to_match_regex("id", regex=r"^C\d{5}$")
    qa.add_sql("SELECT * FROM ${object} WHERE points < 0", description="no negative points")
    qa.add_sql("SELECT AVG(points) FROM ${object}", mustBeBetween=[0, 5000], severity="P3")
    code = qa.to_code()
    assert code.startswith("from redibis.quality import QualityDraft\n\nqa = QualityDraft.new('shop.customers')")
    assert "qa.expect_column_values_to_match_regex('id', regex=r'^C\\d{5}$')" in code
    assert "add_sql('SELECT * FROM ${object} WHERE points < 0', description='no negative points')" in code
    assert "severity='P2'" in code and "severity='P3'" in code
    assert "mostly=1.0" not in code and "strict_min" not in code          # GE defaults left out

    ns: dict = {}
    exec(code, ns)                                                         # noqa: S102 — our own output
    again = ns["qa"]
    assert [(r["column"], r["type"], r["severity"]) for r in again.rules] == \
           [(r["column"], r["type"], r["severity"]) for r in qa.rules]
    verdicts = lambda d: d.validate(_bad()).to_frame()[["expectation", "column", "passed", "unexpected"]]  # noqa: E731
    assert verdicts(again).equals(verdicts(qa))


def test_to_code_in_the_quality_page_style():
    qa = QualityDraft.new("shop.t").expect_column_values_to_not_be_null("id", severity="P2")
    code = qa.to_code(style="gx")
    assert "qa.add_gx_expectation(" in code
    assert "expectation_name='expect_column_values_to_not_be_null'" in code
    assert "column='id'" in code and "severity='P2'" in code
    ns: dict = {}
    exec(code, ns)                                                         # noqa: S102
    assert ns["qa"].rules[0]["severity"] == "P2"
    with pytest.raises(ValueError):
        qa.to_code(style="yaml")


def _auto() -> QualityDraft:
    qa = (QualityDraft.new("shop.t")
          .expect_column_values_to_be_between("points", min_value=0, max_value=10)
          .expect_column_values_to_not_be_null("id"))
    qa.add_sql("SELECT * FROM ${object} WHERE points < 0", description="no negative points")
    return qa


def _curated() -> QualityDraft:
    qa = (QualityDraft.new()                                   # a quick-check draft merges anywhere
          .expect_column_values_to_be_between("points", min_value=0, max_value=5000, severity="P2")
          .expect_column_values_to_not_be_null("id")            # identical: not duplicated
          .expect_column_values_to_be_unique("id"))
    qa.add_sql("SELECT *  FROM ${object}  WHERE points < 0", severity="P3")   # same query, other spacing
    return qa


def _between(qa):
    return [(r["params"]["max_value"], r["severity"]) for r in qa.rules
            if r["expectation_type"] == "expect_column_values_to_be_between"]


def test_merge_the_draft_merged_in_wins_conflicts_by_default():
    auto = _auto()
    assert auto.merge(_curated()) is auto
    assert _between(auto) == [(5000, "P2")]
    assert [r["type"] for r in auto.rules if r["column"] == "id"] == ["not_null", "unique"]
    sql = [r for r in auto.rules if r["type"] == "sql"]
    assert len(sql) == 1 and sql[0]["severity"] == "P3"
    assert auto.validate(_frame()).status == "success"           # the scanned max 10 is gone


def test_merge_keep_and_both():
    kept = _auto().merge(_curated(), on_conflict="keep")
    assert _between(kept) == [(10, "P1")]
    assert len([r for r in kept.rules if r["type"] == "sql"]) == 1
    assert [r["type"] for r in kept.rules if r["column"] == "id"] == ["not_null", "unique"]
    both = _auto().merge(_curated(), on_conflict="both")
    assert sorted(_between(both)) == [(10, "P1"), (5000, "P2")]
    assert len([r for r in both.rules if r["type"] == "sql"]) == 2
    with pytest.raises(ValueError, match="on_conflict"):
        _auto().merge(_curated(), on_conflict="newest")


def test_merge_function_returns_a_new_draft_and_checks_tables():
    auto, curated = _auto(), _curated()
    before = (auto.to_code(), curated.to_code())
    merged = merge(auto, curated)
    assert merged.table == "shop.t" and _between(merged) == [(5000, "P2")]
    assert (auto.to_code(), curated.to_code()) == before            # inputs unchanged
    with pytest.raises(ValueError, match="cannot merge rules for 'other.t'"):
        _auto().merge(QualityDraft.new("other.t"))
    with pytest.raises(TypeError):
        _auto().merge(_curated().to_rules())
