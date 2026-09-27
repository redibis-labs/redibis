"""Quality rule authoring from code (pandas / Spark) + the `redibis quality-run` review cycle."""

from __future__ import annotations

import copy
import logging
import shutil
import subprocess
from pathlib import Path

import pandas as pd
import pytest
import yaml

pytest.importorskip("great_expectations")

from redibis.cli.main import main as cli
from redibis.contracts.rules import _rule_to_ge, extract_rules
from redibis.contracts.type_inference import dtype_map_from_dataframe, infer_types_from_dtype
from redibis.quality.authoring import (
    SNAPSHOT_RULES,
    QualityAuthor,
    QualityDraft,
    author_quality,
    diff_rules,
    drop_rules,
    ensure_column_coverage,
    list_rules,
    relax_rules,
)

CSV = Path(__file__).parent / "data" / "realistic_eshop_customer_account.csv"
TABLE = "eshop.customer_account"


@pytest.fixture(scope="module")
def draft() -> QualityDraft:
    logging.getLogger("great_expectations").setLevel(logging.ERROR)
    df = pd.read_csv(CSV, dtype=str).head(300)
    return author_quality(df, TABLE)


def _payload(**columns) -> dict:
    return {
        "apiVersion": "v3.0.1", "kind": "DataContract",
        "database_name": "d", "table_name": "t",
        "schema": [{"name": "t", "properties": [
            {"name": c, "physicalType": "string", "logicalType": "string", "quality": q}
            for c, q in columns.items()
        ]}],
    }


# ── authoring (pandas) ───────────────────────────────────────────────────────

def test_author_discovers_validates_and_covers_every_column(draft):
    assert draft.engine == "pandas" and draft.rows == 300
    assert draft.total > 0 and draft.passed == draft.total
    covered = {r["column"] for r in draft.rules if r["column"]}
    # the onboarding assistant skips some ID columns; coverage fills them in
    assert {"eshop_customer_id", "session_id"} <= covered
    id_rules = {r["type"] for r in draft.rules if r["column"] == "eshop_customer_id"}
    assert {"not_null", "unique"} <= id_rules


def test_list_rules_numbering_matches_extract_rules(draft):
    rules = list_rules(draft.payload)
    assert len(rules) == len(extract_rules(draft.payload))
    assert [r["index"] for r in rules] == list(range(len(rules)))


def test_regex_patterns_survive_export(draft):
    regex = [r for r in draft.rules if r["type"] == "regex"]
    assert regex, "profiler should propose at least one format rule"
    assert all(r["params"].get("arguments", {}).get("pattern") for r in regex)


def test_draft_yaml_roundtrip_and_bare_partial(draft, tmp_path):
    path = draft.to_yaml(tmp_path / "rules.yaml")
    again = QualityDraft.from_yaml(path)
    assert again.table == TABLE and again.rules == draft.rules
    bare = tmp_path / "bare.yaml"
    bare.write_text(yaml.safe_dump(draft.payload), encoding="utf-8")
    assert QualityDraft.from_yaml(bare).table == TABLE


def test_draft_validate_against_another_frame(draft):
    other = pd.read_csv(CSV, dtype=str).iloc[300:600]
    curated = copy.deepcopy(draft)
    curated.drop(rule_types=SNAPSHOT_RULES)
    curated.drop(columns=["full_name"])  # valid names differ between slices
    result = curated.validate(other)
    assert result.rules_total > 0
    assert result.rules_passed >= result.rules_total - 3


# ── pure helpers ─────────────────────────────────────────────────────────────

def test_drop_rules_by_column_type_alias_and_index():
    p = _payload(
        a=[{"rule": "missingCount", "mustBe": 0},
           {"rule": "validValues", "arguments": {"validValues": ["x"]}}],
        b=[{"rule": "regex", "arguments": {"pattern": "^b"}}],
    )
    _, removed = drop_rules(p, rule_types=["in_set"])
    assert [r["column"] for r in removed] == ["a"] and removed[0]["type"] == "set"
    _, removed = drop_rules(p, rule_types=["validValues"])
    assert len(removed) == 1
    new, removed = drop_rules(p, columns=["b"], indices=[0])
    assert len(removed) == 2 and len(list_rules(new)) == 1
    assert len(list_rules(p)) == 3, "input payload must not be mutated"


def test_relax_widens_numbers_only():
    p = _payload(amount=[
        {"rule": "uniqueCount", "mustBeBetween": [10, 10]},
        {"engine": "greatExpectations", "implementation": {
            "expectation_type": "expect_column_mean_to_be_between",
            "kwargs": {"column": "amount", "min_value": 100.0, "max_value": 100.0}}},
        {"engine": "greatExpectations", "implementation": {
            "expectation_type": "expect_column_value_lengths_to_be_between",
            "kwargs": {"column": "amount", "min_value": 3, "max_value": 6}}},
        {"engine": "greatExpectations", "implementation": {
            "expectation_type": "expect_column_proportion_of_unique_values_to_be_between",
            "kwargs": {"column": "amount", "min_value": 0.95, "max_value": 1.0}}},
    ])
    new, changed = relax_rules(p, 0.1)
    q = new["schema"][0]["properties"][0]["quality"]
    assert changed == 3
    assert q[0]["mustBeBetween"] == [9, 11]
    assert q[1]["implementation"]["kwargs"]["min_value"] == 90.0
    assert q[1]["implementation"]["kwargs"]["max_value"] == 110.0
    assert q[2]["implementation"]["kwargs"]["min_value"] == 3, "value lengths are structural"
    assert q[3]["implementation"]["kwargs"]["max_value"] == 1.0, "proportions stay within [0, 1]"


def test_relax_keeps_sign_and_rounds_so_near_identical_ranges_match():
    def between(lo, hi):
        return _payload(amount=[{"engine": "greatExpectations", "implementation": {
            "expectation_type": "expect_column_values_to_be_between",
            "kwargs": {"column": "amount", "min_value": lo, "max_value": hi}}}])

    def bounds(p):
        kw = relax_rules(p, 0.1)[0]["schema"][0]["properties"][0]["quality"][0]
        kw = kw["implementation"]["kwargs"]
        return kw["min_value"], kw["max_value"]

    assert bounds(between(0.0, 4999.98)) == (0.0, 5500.0), "a non-negative range stays >= 0"
    assert bounds(between(0.0, 4999.98)) == bounds(between(0.0, 5000.0)), "no diff churn"
    assert bounds(between(-20.0, -5.0)) == (-22.0, -3.0), "a non-positive range stays <= 0"


def test_value_lists_are_sorted_so_reordering_is_not_a_change():
    from redibis.quality.authoring import _canonicalize

    a = _canonicalize(_payload(c=[{"rule": "validValues",
                                   "arguments": {"validValues": ["USD", "EGP"]}}]))
    b = _canonicalize(_payload(c=[{"rule": "validValues",
                                   "arguments": {"validValues": ["EGP", "USD"]}}]))
    assert list_rules(a)[0]["params"] == list_rules(b)[0]["params"]
    change = diff_rules(a, b)[0]
    assert not change["added"] and not change["removed"]


def test_diff_replaces_rules_only_for_columns_in_the_run():
    active = _payload(a=[{"rule": "missingCount", "mustBe": 0}],
                      b=[{"rule": "missingCount", "mustBe": 0}])
    run = _payload(a=[{"rule": "duplicateCount", "mustBe": 0}])
    changes = {c["column"]: c for c in diff_rules(active, run)}
    assert set(changes) == {"a"}, "column b is not in the run, so it is untouched"
    assert [r["type"] for r in changes["a"]["added"]] == ["unique"]
    assert [r["type"] for r in changes["a"]["removed"]] == ["not_null"]


def test_diff_reports_rules_the_privacy_guard_will_not_store(monkeypatch):
    monkeypatch.delenv("REDIBIS_PII_QUALITY_KEEP_SAFE", raising=False)
    monkeypatch.delenv("REDIBIS_KEEP_QUALITY_ON_PII", raising=False)
    active = _payload(msisdn=[], note=[])
    active["schema"][0]["properties"][0]["privacy"] = {"classification": "pii_personal"}
    run = _payload(
        msisdn=[{"rule": "missingCount", "mustBe": 0},
                {"rule": "regex", "arguments": {"pattern": "^01"}}],
        note=[{"rule": "missingCount", "mustBe": 0}],
    )
    changes = {c["column"]: c for c in diff_rules(active, run)}
    assert not changes["msisdn"]["added"] and len(changes["msisdn"]["withheld"]) == 2
    assert len(changes["note"]["added"]) == 1

    monkeypatch.setenv("REDIBIS_PII_QUALITY_KEEP_SAFE", "1")
    changes = {c["column"]: c for c in diff_rules(active, run)}
    assert [r["type"] for r in changes["msisdn"]["added"]] == ["not_null"]
    assert [r["type"] for r in changes["msisdn"]["withheld"]] == ["regex"]


def test_coverage_adds_key_rules_for_uncovered_columns():
    df = pd.DataFrame({"id": ["A1", "A2", "A3"], "maybe": ["x", None, "y"]})
    payload, covered, rows = ensure_column_coverage(df, _payload(), spark=False)
    assert covered == ["id", "maybe"] and rows == 3
    props = {p["name"]: p for p in payload["schema"][0]["properties"]}
    assert props["id"]["unique"] and props["id"]["required"]
    assert [q.get("rule") for q in props["maybe"]["quality"]] == [None]  # lengths only


def test_regex_pattern_round_trips_to_great_expectations():
    from redibis.contracts.mapper import _map_match_regex

    q = _map_match_regex({"regex": "^01[0-9]{9}$"})
    assert q["arguments"]["pattern"] == "^01[0-9]{9}$"
    rule = extract_rules(_payload(msisdn=[q]))[0]
    assert _rule_to_ge(rule)["kwargs"]["regex"] == "^01[0-9]{9}$"
    legacy = extract_rules(_payload(msisdn=[{"rule": "regex", "pattern": "^x$"}]))[0]
    assert _rule_to_ge(legacy)["kwargs"]["regex"] == "^x$"


def test_spark_type_strings_map_to_odcs_types():
    assert infer_types_from_dtype("bigint") == ("bigint", "integer")
    assert infer_types_from_dtype("decimal(12,2)")[1] == "decimal"
    assert infer_types_from_dtype("map<string,int>")[1] == "object"
    assert infer_types_from_dtype("struct<a:int>")[1] == "object"
    assert infer_types_from_dtype("array<string>")[1] == "array"

    class _Type:
        def __init__(self, s): self.s = s
        def simpleString(self): return self.s

    class _Field:
        def __init__(self, n, t): self.name, self.dataType = n, _Type(t)

    class FakeSpark:
        sparkSession = object()
        schema = type("S", (), {"fields": [_Field("id", "bigint"), _Field("ts", "timestamp")]})()

    assert dtype_map_from_dataframe(FakeSpark()) == {"id": "bigint", "ts": "timestamp"}


# ── code ⇄ CLI cycle ─────────────────────────────────────────────────────────

def test_code_save_then_cli_review_edit_merge(draft, tmp_path, capsys):
    out = str(tmp_path)
    qa = QualityAuthor(TABLE, output_dir=out)
    run = qa.save(draft, run_id="nb1", note="from notebook")
    assert run.status == "draft"

    assert cli(["quality-run", "show", TABLE, "--output-dir", out]) == 0
    shown = capsys.readouterr().out
    assert "run nb1" in shown and "[  0]" in shown

    assert cli(["quality-run", "diff", TABLE, "--output-dir", out]) == 0
    assert "added" in capsys.readouterr().out

    assert cli(["quality-run", "edit", TABLE, "--run", "nb1", "--drop-rule", "in_set",
                "--relax", "0.1", "--note", "curated", "--output-dir", out]) == 0
    edited = qa.load("nb1")
    assert not any(r["type"] == "set" for r in edited.rules)
    sub = qa.runs_store.get("quality", TABLE, "nb1")
    assert sub.status == "reviewed" and sub.edits[-1].note == "curated"

    assert cli(["quality-run", "merge", TABLE, "--run", "nb1",
                "--output-dir", out]) == 0
    capsys.readouterr()
    assert len(qa.active_rules()) == len(edited.rules)
    assert "No rule changes" in qa.diff("nb1")


def test_file_handoff_import_export_and_edit_from_file(draft, tmp_path, capsys):
    out = str(tmp_path / "store")
    rules = draft.to_yaml(tmp_path / "rules.yaml")
    assert cli(["quality-run", "import", TABLE, str(rules), "--run", "imp1",
                "--output-dir", out]) == 0
    assert "Imported" in capsys.readouterr().out

    exported = tmp_path / "edit_me.yaml"
    assert cli(["quality-run", "export", TABLE, "--run", "imp1", "-o", str(exported),
                "--output-dir", out]) == 0
    data = yaml.safe_load(exported.read_text(encoding="utf-8"))
    data["payload"]["schema"][0]["properties"] = data["payload"]["schema"][0]["properties"][:1]
    exported.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert cli(["quality-run", "edit", TABLE, "--run", "imp1", "--file", str(exported),
                "--output-dir", out]) == 0
    capsys.readouterr()
    left = QualityAuthor(TABLE, output_dir=out).load("imp1")
    assert {r["column"] for r in left.rules if r["column"]} == {
        data["payload"]["schema"][0]["properties"][0]["name"]}


def test_cli_refuses_wrong_table_and_empty_edit(draft, tmp_path, capsys):
    out = str(tmp_path)
    rules = draft.to_yaml(tmp_path / "rules.yaml")
    assert cli(["quality-run", "import", "other.table", str(rules), "--output-dir", out]) == 1
    assert "not 'other.table'" in capsys.readouterr().err
    QualityAuthor(TABLE, output_dir=out).save(draft, run_id="r1")
    assert cli(["quality-run", "edit", TABLE, "--run", "r1", "--output-dir", out]) == 1
    assert "Nothing to do" in capsys.readouterr().err
    assert cli(["quality-run", "show", "no.such_table", "--output-dir", out]) == 1


# ── Spark ────────────────────────────────────────────────────────────────────

def _java_ok() -> bool:
    if not shutil.which("java"):
        return False
    try:
        return subprocess.run(["java", "-version"], capture_output=True, timeout=30).returncode == 0
    except Exception:
        return False


@pytest.mark.slow
def test_author_on_a_spark_dataframe(tmp_path):
    pytest.importorskip("pyspark")
    if not _java_ok():
        pytest.skip("Spark needs a Java runtime")
    from pyspark.sql import SparkSession, functions as F

    spark = (SparkSession.builder.master("local[2]").appName("redibis-authoring-test")
             .config("spark.ui.enabled", "false").getOrCreate())
    try:
        df = (spark.range(5000)
              .withColumn("txn_id", F.format_string("TXN-%06d", F.col("id")))
              .withColumn("currency", F.element_at(
                  F.array(F.lit("EGP"), F.lit("USD")), (F.col("id") % 2 + 1).cast("int")))
              .withColumn("amount", (F.col("id") % 97).cast("decimal(10,2)"))
              .drop("id"))
        qa = QualityAuthor("sales.transactions", output_dir=str(tmp_path))
        d = qa.author(df, log=None)
        assert d.engine == "spark" and d.rows == 5000 and d.total > 0
        cols = {r["column"] for r in d.rules if r["column"]}
        assert {"txn_id", "currency", "amount"} <= cols
        types = {p["name"]: p.get("logicalType") for p in d.payload["schema"][0]["properties"]}
        assert types["amount"] == "decimal"
        d.drop(rule_types=SNAPSHOT_RULES)
        assert d.validate(df).status == "success"
        run = qa.save(d)
        qa.merge(run.run_id)
        assert qa.active_rules()
    finally:
        spark.stop()


# ── Quality page program ⇄ notebook ⇄ quality-run ─────────────────────────────

UI_RULES = [
    {"rule": "expect_column_values_to_not_be_null", "column": "eshop_customer_id",
     "kwargs": {"column": "eshop_customer_id"}},
    {"rule": "expect_column_value_lengths_to_be_between", "column": "mobile_number",
     "kwargs": {"column": "mobile_number", "min_value": 11, "max_value": 11}},
]


def _program(engine: str = "pandas") -> str:
    from redibis.services.quality_code import CuratedRules, render_quality_program

    return render_quality_program(
        table=TABLE, curated=CuratedRules(rules=UI_RULES, rule_source="session_draft"),
        engine=engine)


@pytest.mark.parametrize("engine", ["pandas", "spark"])
def test_generated_program_is_complete_and_continues_in_redibis(engine):
    code = _program(engine)
    compile(code, f"{engine}_program", "exec")
    for needle in ("from redibis.quality.authoring import", "def validate(",
                   "def save_quality_run(", "def discover_rules(", "def add_rules(",
                   'if __name__ == "__main__" and not _in_notebook():'):
        assert needle in code, needle


def test_pasting_the_program_into_a_notebook_does_not_run_main():
    ns = {"__name__": "__main__", "get_ipython": lambda: object()}
    exec(compile(_program("pandas"), "cell", "exec"), ns)  # must not SystemExit
    assert callable(ns["validate"]) and ns["TABLE"] == TABLE


def test_program_still_runs_as_a_script_outside_notebooks(monkeypatch):
    monkeypatch.setattr("sys.argv", ["prog", "--sample", str(CSV), "--rows", "50"])
    with pytest.raises(SystemExit) as exc:
        exec(compile(_program("pandas"), "script", "exec"), {"__name__": "__main__"})
    assert exc.value.code == 0


def test_program_validate_edit_discover_and_save_a_quality_run(tmp_path, capsys):
    ns = {"__name__": "notebook_cell"}
    exec(compile(_program("pandas"), "cell", "exec"), ns)
    df = pd.read_csv(CSV, dtype=str).head(200)
    assert ns["validate"](df)["statistics"]["successful_expectations"] == 2
    more = ns["discover_rules"](df)
    more.drop(rule_types=SNAPSHOT_RULES)
    before = len(ns["RULES"])
    added = ns["add_rules"](more)
    assert added > 0 and len(ns["RULES"]) == before + added
    assert ns["add_rules"](more) == 0, "re-adding the same rules is a no-op"
    run_id = ns["save_quality_run"](df, output_dir=str(tmp_path), note="nb")
    out = capsys.readouterr().out
    assert f"quality-run diff {TABLE} --run {run_id}" in out
    stored = QualityAuthor(TABLE, output_dir=str(tmp_path)).load(run_id)
    assert stored.passed == stored.total > 0


def test_draft_program_round_trip(draft, tmp_path):
    from redibis.quality.authoring import draft_from_rules

    code = draft.to_program("spark")
    back = QualityDraft.from_program(code)
    assert back.table == TABLE
    assert len(back.to_rules()) == len(draft.to_rules())
    packaged = draft_from_rules(TABLE, UI_RULES)
    assert {r["column"] for r in packaged.rules} == {"eshop_customer_id", "mobile_number"}


def test_quality_run_cli_exports_and_imports_programs(draft, tmp_path, capsys):
    out = str(tmp_path / "store")
    QualityAuthor(TABLE, output_dir=out).save(draft, run_id="r1")
    program = tmp_path / "eshop_quality.py"
    assert cli(["quality-run", "export", TABLE, "--run", "r1", "-o", str(program),
                "--output-dir", out]) == 0
    compile(program.read_text(encoding="utf-8"), "exported", "exec")
    assert cli(["quality_run", "import", TABLE, str(program), "--run", "from_py",
                "--output-dir", out]) == 0          # underscore alias works too
    assert cli(["quality-run", "list", TABLE, "--output-dir", out]) == 0
    listing = capsys.readouterr().out
    assert "r1" in listing and "from_py" in listing
    assert cli(["quality-run", "merge", TABLE, "--run", "from_py", "--output-dir", out]) == 0
    assert cli(["quality-run", "discard", TABLE, "--run", "r1", "--output-dir", out]) == 0
    capsys.readouterr()


def test_runs_command_no_longer_carries_quality_authoring():
    with pytest.raises(SystemExit):
        cli(["runs", "show", TABLE])


@pytest.mark.slow
def test_program_runs_in_a_real_jupyter_kernel(tmp_path):
    nbformat = pytest.importorskip("nbformat")
    nbclient = pytest.importorskip("nbclient")
    pytest.importorskip("ipykernel")
    cells = [
        _program("pandas"),
        f"import pandas as pd\ndf = pd.read_csv({str(CSV)!r}, dtype=str).head(200)",
        "print('passed', validate(df)['statistics']['successful_expectations'])",
        f"run_id = save_quality_run(df, output_dir={str(tmp_path)!r})",
    ]
    nb = nbformat.v4.new_notebook(cells=[nbformat.v4.new_code_cell(c) for c in cells])
    client = nbclient.NotebookClient(nb, timeout=600, kernel_name="python3",
                                     resources={"metadata": {"path": str(Path.cwd())}})
    client.execute()
    errors = [o for c in nb.cells for o in c.get("outputs", []) if o.output_type == "error"]
    assert not errors, errors
    printed = "".join(o.get("text", "") for c in nb.cells for o in c.get("outputs", [])
                      if o.output_type == "stream")
    assert "passed 2" in printed and "saved quality run" in printed
    assert QualityAuthor(TABLE, output_dir=str(tmp_path)).runs()
