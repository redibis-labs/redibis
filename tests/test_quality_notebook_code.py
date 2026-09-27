"""Quality page "Copy Jupyter Code": QualityDraft-style notebook code, paste-back, approval UI."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pandas as pd
import pytest

pytest.importorskip("great_expectations")

from redibis.contracts.rule_code_parser import parse_ge_rules
from redibis.quality.authoring import draft_from_rules
from redibis.services.quality_code import CuratedRules, render_notebook_code
from redibis.services.session.approved import quality_row_to_fragment

REPO = Path(__file__).resolve().parents[1]
RULES = [
    {"rule": "expect_column_values_to_not_be_null", "column": "id", "kwargs": {}},
    {"rule": "expect_column_values_to_be_between", "column": "age",
     "kwargs": {"min_value": 0, "max_value": 120}, "meta": {"severity": "P2"}},
    {"rule": "expect_column_values_to_be_in_set", "column": "country",
     "kwargs": {"value_set": ["EG", "SA"]}},
    {"rule": "sql", "column": None, "description": "no one over 150",
     "kwargs": {"sql": "SELECT * FROM ${object} WHERE age > 150", "mustBeLessThan": 1}},
]


@pytest.fixture(autouse=True)
def _quiet():
    logging.getLogger("great_expectations").setLevel(logging.ERROR)


@pytest.fixture()
def csv_path(tmp_path) -> str:
    path = tmp_path / "customers.csv"
    pd.DataFrame({"id": ["C1", "C2", None, "C4"], "age": [30, None, 45, 200],
                  "country": ["EG", "SA", "XX", None]}).to_csv(path, index=False)
    return str(path)


def _curated() -> CuratedRules:
    return CuratedRules(rules=[dict(r) for r in RULES], rule_source="session_draft")


def test_notebook_code_reads_the_file_writes_the_rules_and_validates(csv_path):
    code = render_notebook_code(table="shop.customers", curated=_curated(), data_path=csv_path)
    for needle in ("from pyspark.sql import SparkSession", "from redibis.quality import QualityDraft",
                   f"DATA = {csv_path!r}", "pd.read_csv(DATA)", "spark.createDataFrame(",
                   "qa = QualityDraft.new('shop.customers')",
                   "qa.expect_column_values_to_not_be_null('id')",
                   "qa.expect_column_values_to_be_between('age', min_value=0, max_value=120, mostly=1.0, severity='P2')",
                   "qa.add_sql('SELECT * FROM ${object} WHERE age > 150', description='no one over 150')",
                   'qa.validate(df, spark_mode="fused")'):
        assert needle in code, needle
    compile(code, "notebook.py", "exec")

    pandas_code = render_notebook_code(table="shop.customers", curated=_curated(),
                                       engine="pandas", data_path=csv_path)
    assert "pyspark" not in pandas_code
    ns: dict = {}
    exec(pandas_code, ns)                                                  # noqa: S102 — our own output
    result = ns["result"]
    assert result.rules_total == 4 and result.rules_passed == 0
    by = {r.expectation_type: r for r in result.results}
    assert by["expect_column_values_to_be_between"].severity == "P2"


def test_rules_only_code_applies_to_any_dataframe(csv_path):
    code = render_notebook_code(table="shop.customers", curated=_curated(), style="rules")
    assert "DATA =" not in code and "read_csv" not in code
    assert "result = qa.validate(df)" in code
    ns: dict = {"df": pd.read_csv(csv_path)}
    exec(code, ns)                                                         # noqa: S102
    assert ns["result"].rules_total == 4
    with pytest.raises(ValueError, match="style"):
        render_notebook_code(table="t.x", curated=_curated(), style="yaml")


def test_generated_code_parses_back_into_the_same_rules(csv_path):
    for style in ("notebook", "rules"):
        code = render_notebook_code(table="shop.customers", curated=_curated(), style=style,
                                    data_path=csv_path)
        parsed = parse_ge_rules(code)
        assert parsed["errors"] == []
        got = {(r["expectation_type"], r["column"]): r for r in parsed["rules"]}
        assert set(got) == {("expect_column_values_to_not_be_null", "id"),
                            ("expect_column_values_to_be_between", "age"),
                            ("expect_column_values_to_be_in_set", "country"), ("sql", None)}
        assert got[("expect_column_values_to_be_between", "age")]["meta"] == {"severity": "P2"}
        assert got[("sql", None)]["kwargs"]["sql"] == "SELECT * FROM ${object} WHERE age > 150"


def test_parser_accepts_the_column_first_and_still_rejects_code():
    parsed = parse_ge_rules(
        "(qa.expect_column_values_to_be_unique('id')\n"
        "   .expect_column_values_to_not_be_null('email', severity='P3'))\n"
        "qa.add_sql('SELECT * FROM ${object} WHERE x < 0', severity='P2')\n"
        "qa.expect_column_values_to_be_unique('a', column='b')\n"
        "qa.expect_column_values_to_be_unique(__import__('os').getcwd())\n"
        "qa.add_gx_expectation('expect_column_values_to_be_unique', column='c')\n")
    assert [(r["expectation_type"], r["column"]) for r in parsed["rules"]] == [
        ("expect_column_values_to_be_unique", "id"),          # a chain keeps source order
        ("expect_column_values_to_not_be_null", "email"),
        ("sql", None)]
    assert parsed["rules"][1]["meta"] == {"severity": "P3"}
    assert parsed["rules"][2]["meta"] == {"severity": "P2"}
    assert len(parsed["errors"]) == 3


def test_severity_travels_to_the_contract_and_through_draft_from_rules():
    frag = quality_row_to_fragment({"expectation_type": "expect_column_values_to_be_unique",
                                    "kwargs": {"column": "id"}, "meta": {"severity": "P2"}})
    assert frag == {"severity": "P2", "rule": "duplicateCount", "mustBe": 0}
    draft = draft_from_rules("shop.customers", [dict(r) for r in RULES])
    sev = {(r["column"], r["type"]): r["severity"] for r in draft.rules}
    assert sev[("age", "engine")] == "P2" and sev[("id", "not_null")] == "P1"


def test_full_code_endpoint_serves_the_notebook_style(monkeypatch):
    from fastapi.testclient import TestClient

    from redibis.webapp import backend

    class _NoContracts:                     # keep the test off the real contract store
        quality_decisions = None

        def get_active(self, table):
            return None

    monkeypatch.setattr(backend, "get_contract_store", lambda: _NoContracts())
    client = TestClient(backend.app)
    import io

    buf = io.BytesIO()
    pd.DataFrame({"id": [1, 2], "phone": ["555-0100", None]}).to_csv(buf, index=False)
    buf.seek(0)
    sid = client.post("/api/sessions", files={"file": ("sample.csv", buf, "text/csv")},
                      data={"table": "shop.phones", "scan_mode": "quality"}).json()["session_id"]
    rules = [{"rule": "expect_column_values_to_not_be_null", "column": "id", "kwargs": {}}]
    r = client.post(f"/api/sessions/{sid}/quality/full-code",
                    json={"rules": rules, "style": "notebook", "engine": "spark"})
    assert r.status_code == 200, r.text
    assert "qa.expect_column_values_to_not_be_null('id')" in r.text
    session = backend._require_session(sid)
    assert f"DATA = {os.path.abspath(str(session.data_path))!r}" in r.text   # the uploaded file
    r = client.post(f"/api/sessions/{sid}/quality/full-code", json={"rules": rules, "style": "rules"})
    assert r.status_code == 200 and "result = qa.validate(df)" in r.text
    r = client.post(f"/api/sessions/{sid}/quality/full-code", json={"rules": rules})
    assert "RULES: list[dict] = [" in r.text                               # default: the classic program
    assert client.post(f"/api/sessions/{sid}/quality/full-code",
                       json={"rules": rules, "style": "zip"}).status_code == 400


def test_quality_pages_stage_approvals_until_sent():
    js = (REPO / "redibis/webapp/static/app.js").read_text(encoding="utf-8")
    css = (REPO / "redibis/webapp/static/app.css").read_text(encoding="utf-8")
    for needle in ("function sendStagedQuality", "function toggleQualityRule", "function qualitySendBar",
                   "qrow-pending", "qrow-approved", "✓ sent · withdraw", "codeStyleSel", "codeScopeSel",
                   'style:"notebook"'):
        assert needle in js, needle
    assert ".tbl tbody tr.qrow-pending" in css and ".tbl tbody tr.qrow-approved" in css
    # The only place a rule reaches the Approved basket from the quality tables is the send action.
    send = js[js.index("async function sendStagedQuality"):]
    send = send[:send.index("\n}\n")]
    assert "/approved/quality" in send
    batch = js[js.index("async function approveQualityBatch"):]
    batch = batch[:batch.index("\n}\n")]
    assert "/approved/quality" not in batch


# ── the code follows the page and its approvals ──────────────────────────────

def test_code_uses_the_last_quality_run_even_when_a_pii_run_came_after():
    """A scan ends with its PII run; the code used to skip the quality run and fall back
    to the contract (the same merged rules whatever was approved on the page)."""
    from types import SimpleNamespace

    from redibis.services.quality_code import resolve_curated_rules

    quality = SimpleNamespace(quality_results=[
        {"rule": "expect_column_values_to_match_regex", "column": "code", "kwargs": {"regex": r"\d+", "mostly": 1.0}}])
    session = SimpleNamespace(quality_rules_draft=None, runs=[quality, SimpleNamespace(quality_results=[])],
                              table_name="shop.t")
    curated = resolve_curated_rules(session=session, table="shop.t", store=None)
    assert curated.rule_source == "session_run"
    assert curated.effective_rules[0]["kwargs"]["regex"] == r"\d+"


def test_rule_lines_show_the_tolerance_and_every_parameter():
    code = render_notebook_code(table="shop.t", style="rules", curated=CuratedRules(rule_source="x", rules=[
        {"rule": "expect_column_values_to_be_unique", "column": "id", "kwargs": {}},
        {"rule": "expect_column_values_to_not_be_null", "column": "id", "kwargs": {"mostly": 1.0}},
        {"rule": "expect_column_value_lengths_to_be_between", "column": "id",
         "kwargs": {"min_value": 4, "max_value": 8, "mostly": 1.0, "strict_min": False, "strict_max": False}},
        {"rule": "expect_column_values_to_match_regex", "column": "id", "kwargs": {"regex": r"\d+", "mostly": 0.95}},
        {"rule": "expect_column_values_to_be_in_set", "column": "c", "kwargs": {"value_set": ["EG"]}},
        {"rule": "expect_column_min_to_be_between", "column": "n", "kwargs": {"min_value": 1, "max_value": 1}},
    ]))
    for line in ("qa.expect_column_values_to_be_unique('id')",
                 "qa.expect_column_values_to_not_be_null('id')",
                 "qa.expect_column_value_lengths_to_be_between('id', min_value=4, max_value=8, mostly=1.0)",
                 "qa.expect_column_values_to_match_regex('id', regex=r'\\d+', mostly=0.95)",
                 "qa.expect_column_values_to_be_in_set('c', value_set=['EG'], mostly=1.0)",
                 "qa.expect_column_min_to_be_between('n', min_value=1, max_value=1)"):
        assert line in code, line


def test_regex_and_value_set_tolerance_survive_the_contract():
    from redibis.services.session.approved import quality_row_to_fragment
    from redibis.services.quality_code import _normalize_rule

    for etype, kwargs in (("expect_column_values_to_match_regex", {"regex": r"\d+", "mostly": 0.95}),
                          ("expect_column_values_to_be_in_set", {"value_set": ["a"], "mostly": 0.9})):
        fragment = quality_row_to_fragment({"expectation_type": etype, "kwargs": dict(kwargs, column="c")})
        back = _normalize_rule(dict(fragment, column="c"))
        assert back["rule"] == etype and back["kwargs"] == kwargs


def test_full_code_header_names_the_page_scope(monkeypatch):
    from fastapi.testclient import TestClient

    from redibis.webapp import backend

    class _NoContracts:
        quality_decisions = None

        def get_active(self, table):
            return None

    monkeypatch.setattr(backend, "get_contract_store", lambda: _NoContracts())
    client = TestClient(backend.app)
    import io

    buf = io.BytesIO(b"id\n1\n2\n")
    sid = client.post("/api/sessions", files={"file": ("s.csv", buf, "text/csv")},
                      data={"table": "shop.ids"}).json()["session_id"]
    rules = [{"rule": "expect_column_values_to_not_be_null", "column": "id", "kwargs": {}}]
    r = client.post(f"/api/sessions/{sid}/quality/full-code",
                    json={"rules": rules, "style": "notebook", "label": "approved"})
    assert r.headers["X-Redibis-Rule-Source"] == "approved rules"
    assert "1 rule(s) from approved rules" in r.text


def test_copy_code_is_rebuilt_from_the_page_on_every_click():
    js = (REPO / "redibis/webapp/static/app.js").read_text(encoding="utf-8")
    copy = js[js.index("async function copyFullQualityCode"):]
    copy = copy[:copy.index("\n}\n")]
    assert "approvedQualityRulesForCode()" in copy and "qualityRulesForScope(" in copy
    approved = js[js.index("function approvedQualityRulesForCode"):]
    approved = approved[:approved.index("\n}\n")]
    assert "isQualityRuleStaged" in approved and "isQualityRuleApproved" in approved
    assert "copyFullQualityCode({review:true})" in js      # the review page keeps its own draft
