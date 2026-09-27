"""Fused Spark validation: aggregate rules in one job, same verdicts as Great Expectations."""

from __future__ import annotations

import logging
import shutil
import subprocess

import pandas as pd
import pytest

pytest.importorskip("great_expectations")
pytest.importorskip("pyspark")

from redibis.quality import QualityDraft
from redibis.quality.results import get_result_store, validate_partition
from redibis.quality.spark_fused import FUSED_EXPECTATIONS, compile_rule, run_fused

pytestmark = pytest.mark.slow


def _java_ok() -> bool:
    if not shutil.which("java"):
        return False
    try:
        return subprocess.run(["java", "-version"], capture_output=True, timeout=30).returncode == 0
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture(scope="module")
def spark():
    if not _java_ok():
        pytest.skip("Spark needs a Java runtime")
    logging.getLogger("great_expectations").setLevel(logging.ERROR)
    from pyspark.sql import SparkSession

    session = (SparkSession.builder.master("local[2]").appName("redibis-fused-test")
               .config("spark.ui.enabled", "false").getOrCreate())
    yield session


def _pandas(bad: bool) -> pd.DataFrame:
    n = 400
    df = pd.DataFrame({
        "id": [f"C{i:05d}" for i in range(n)],
        "country": (["EG", "SA", "AE", "EG"] * n)[:n],
        "qty": [1 + i % 20 for i in range(n)],
        "price": [100.0 + (i % 50) for i in range(n)],
        "total": [(1 + i % 20) * (100.0 + (i % 50)) for i in range(n)],
        "code": [f"SKU-{i % 90:05d}" for i in range(n)],
    })
    if bad:
        df.loc[:9, "id"] = "C00000"
        df.loc[:4, "country"] = "XX"
        df.loc[:2, "qty"] = 0
        df.loc[:6, "code"] = "sku?"
        df.loc[:3, "total"] = -1.0
        df.loc[10:14, "country"] = None
    return df


def _rules() -> QualityDraft:
    qa = (QualityDraft.new("shop.t")
          .expect_column_values_to_not_be_null("country")
          .expect_column_values_to_be_unique("id")
          .expect_column_values_to_be_in_set("country", value_set=["EG", "SA", "AE"])
          .expect_column_values_to_be_between("qty", min_value=1, max_value=20)
          .expect_column_values_to_match_regex("code", regex=r"^SKU-\d{5}$")
          .expect_column_value_lengths_to_be_between("id", min_value=6, max_value=6)
          .expect_column_min_to_be_between("total", min_value=0)
          .expect_column_mean_to_be_between("price", min_value=110, max_value=140)
          .expect_column_median_to_be_between("price", min_value=110, max_value=140)
          .expect_column_distinct_values_to_be_in_set("country", value_set=["EG", "SA", "AE"])
          .expect_column_unique_value_count_to_be_between("country", min_value=3, max_value=3)
          .expect_column_pair_values_a_to_be_greater_than_b(column_A="total", column_B="qty",
                                                            or_equal=True)
          .expect_table_row_count_to_be_between(min_value=100, max_value=1000)
          .expect_table_columns_to_match_set(column_set=["id", "country", "qty", "price", "total",
                                                         "code"]))
    return qa


def _verdicts(result) -> dict:
    return {(r.expectation_type, r.column): r for r in result.results}


@pytest.mark.parametrize("bad", [False, True], ids=["clean", "defects"])
def test_fused_gives_the_same_verdicts_as_great_expectations(spark, bad):
    df = spark.createDataFrame(_pandas(bad))
    qa = _rules()
    ge = _verdicts(qa.validate(df, spark_mode="ge"))
    fused_result = qa.validate(df, spark_mode="fused")
    fused = _verdicts(fused_result)
    assert fused.keys() == ge.keys()
    for key, g in ge.items():
        f = fused[key]
        assert f.success == g.success, key
        if key[0] != "expect_column_values_to_be_unique":        # fused counts extra copies
            assert f.unexpected_count == g.unexpected_count, key
    assert fused_result.engine == "spark-fused"
    assert fused_result.stats["fused"] == len(ge) and fused_result.stats["ge"] == 0
    assert fused_result.stats["row_count"] == 400
    assert fused_result.stats["spark_jobs_fused"] == (2 if bad else 1)   # +1 job for samples
    assert fused_result.status == ("failed" if bad else "success")


def test_one_aggregate_job_instead_of_several_per_rule(spark):
    df = spark.createDataFrame(_pandas(False)).persist()
    df.count()
    sc = spark.sparkContext
    rules = _rules().to_rules()

    def jobs(group, fn):
        sc.setJobGroup(group, group)
        try:
            fn()
        finally:
            sc.setJobGroup("", "")
        return len(sc.statusTracker().getJobIdsForGroup(group))

    # Adaptive execution reports the shuffle stages of distinct counts as separate jobs;
    # without it the single aggregation is exactly one job.
    aqe = spark.conf.get("spark.sql.adaptive.enabled")
    spark.conf.set("spark.sql.adaptive.enabled", "false")
    try:
        fused_jobs = jobs("fused", lambda: run_fused(df, rules))
        ge_jobs = jobs("ge", lambda: _rules().validate(df, spark_mode="ge"))
    finally:
        spark.conf.set("spark.sql.adaptive.enabled", aqe)
        df.unpersist()
    assert fused_jobs == 1
    assert ge_jobs > 5


def test_failed_rules_carry_samples_and_the_rest_falls_back(spark):
    df = spark.createDataFrame(_pandas(True))
    rules = (_rules()
             .expect_column_values_to_be_increasing("qty")          # needs ordering: Great Expectations
             .to_rules())
    run = run_fused(df, rules)
    assert [r["rule"] for r in run.fallback] == ["expect_column_values_to_be_increasing"]
    by = {(r["rule"], r["column"]): r for r in run.rows}
    regex = by[("expect_column_values_to_match_regex", "code")]
    assert not regex["success"] and regex["unexpected_count"] == 7
    assert regex["partial_unexpected"] and set(regex["partial_unexpected"]) == {"sku?"}
    assert by[("expect_column_mean_to_be_between", "price")]["observed_value"] == pytest.approx(
        _pandas(True)["price"].mean())

    result = QualityDraft.new("shop.t").expect_column_values_to_be_increasing("qty") \
        .expect_column_values_to_not_be_null("id").validate(df, spark_mode="fused")
    assert result.stats == {**result.stats, "fused": 1, "ge": 1}
    assert {r.expectation_type for r in result.results} == {
        "expect_column_values_to_be_increasing", "expect_column_values_to_not_be_null"}


def test_rules_that_do_not_compile_fall_back_instead_of_failing(spark):
    df = spark.createDataFrame(_pandas(False))
    assert compile_rule(df, {"rule": "expect_column_values_to_be_json_parseable", "column": "id",
                             "kwargs": {"column": "id"}}) is None
    assert "expect_column_values_to_be_between" in FUSED_EXPECTATIONS
    run = run_fused(df, [{"rule": "expect_column_values_to_be_between", "column": "nope",
                          "kwargs": {"column": "nope", "min_value": 0}}])
    assert run.rows == [] and len(run.fallback) == 1


def test_persist_mode_leaves_the_frame_as_it_found_it(spark):
    df = spark.createDataFrame(_pandas(False))
    result = _rules().validate(df, spark_mode="persist")
    assert result.engine == "spark-persist" and result.status == "success"
    assert not df.is_cached
    with pytest.raises(ValueError, match="spark_mode"):
        _rules().validate(df, spark_mode="fast")


def test_validate_partition_fused_records_one_row_per_rule(spark, tmp_path):
    store = get_result_store(f"file://{tmp_path}/dq")
    df = spark.createDataFrame(_pandas(True))
    run = validate_partition(df, "shop.t", partition={"dt": "2026-09-26", "hour": 5},
                             contract=_rules().payload, store=store, spark_mode="fused")
    assert run.engine == "spark-fused" and run.row_count == 400        # counted by the same job
    assert (run.partition, run.partition_column, run.partition_value) == (
        "dt=2026-09-26/hour=5", "dt/hour", "2026-09-26/5")
    assert run.partition_ts.isoformat() == "2026-09-26T05:00:00+00:00"
    rows = store.results("shop.t", [run.run_id])
    assert len(rows) == run.rules_total == 14
    assert set(rows["partition_column"]) == {"dt/hour"}
    assert rows["partition_ts"].notna().all()
    assert (~rows["passed"]).sum() == run.rules_total - run.rules_passed


def test_monitor_package_program_runs_fused_and_records_the_partition(spark, tmp_path):
    import types

    from redibis.quality.monitoring_package import build_monitoring_package

    rules = [{"rule": "expect_column_values_to_be_between", "column": "qty",
              "kwargs": {"column": "qty", "min_value": 1, "max_value": 20}, "meta": {"severity": "P2"}},
             {"rule": "expect_column_values_to_be_increasing", "column": "qty", "kwargs": {"column": "qty"}}]
    root = build_monitoring_package(table="shop.t", rules=rules, output_dir=tmp_path)
    program = (root / "shop_t_quality.py").read_text(encoding="utf-8")
    assert 'SPARK_MODE = "fused"' in program and "--results-store" in program
    assert "record_partition" in (root / "README.md").read_text(encoding="utf-8")
    module = types.ModuleType("pkg_program")
    exec(compile(program, "pkg_program.py", "exec"), module.__dict__)        # noqa: S102

    df = spark.createDataFrame(_pandas(True))
    report = module.validate(df)
    assert report["engine"] == "spark-fused" and report["stats"]["fused"] == 1
    assert report["statistics"]["evaluated_expectations"] == 2
    assert module.validate(df, spark_mode="ge")["statistics"]["evaluated_expectations"] == 2

    data = tmp_path / "part.parquet"
    _pandas(True).to_parquet(data)
    store = f"file://{tmp_path}/dq"
    code = module.main(["--path", str(data), "--format", "parquet",
                        "--partition", "dt=2026-08-30", "--results-store", store])
    assert code == 1                                                            # qty 0 rows fail
    run, rows = get_result_store(store).latest("shop.t")
    assert (run.partition_column, run.partition_value, run.engine) == ("dt", "2026-08-30", "spark-fused")
    assert dict(zip(rows["expectation"], rows["severity"]))["expect_column_values_to_be_between"] == "P2"
