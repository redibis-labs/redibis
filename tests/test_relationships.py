"""Tests for native relationship profiling (pandas + Spark)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from redibis.config import SourceConfig
from redibis.models import ColumnProfile
from redibis.profiling.base import ProfileResult
from redibis.profiling.relationships import (
    RELATIONSHIP_KEYS,
    apply_relationship_flags,
    column_relationships,
    normalize_methods,
    profile_relationships,
    resolve_relationship_data,
)
from redibis.quality.rule_set import QualityRuleSet
from redibis.services import pipeline


@pytest.fixture
def rel_df():
    return pd.DataFrame({
        "a": [1, 2, 3, 3],
        "b": [1, 2, 3, 4],
        "c": [5, 5, 5, 5],
        "d": ["x", "x", "x", "x"],
    })


def test_column_relationships_returns_expected_keys(rel_df):
    rel = column_relationships(rel_df)
    assert set(rel.keys()) >= set(RELATIONSHIP_KEYS)
    assert rel["row_count"] == 4
    assert rel["duplicate_rows"] == 0
    assert "c" in rel["constant_columns"]
    assert "d" in rel["constant_columns"]


def test_column_relationships_redundant_pairs(rel_df):
    dup = pd.DataFrame({"x": [1, 2, 3, 1], "y": [10, 20, 30, 10]})
    rel = column_relationships(dup, redundancy_threshold=0.99)
    assert rel["duplicate_rows"] == 1
    assert any(p["a"] == "x" and p["b"] == "y" for p in rel["redundant_pairs"])


def test_normalize_methods_accepts_list():
    assert normalize_methods(["pearson"]) == ("pearson",)


def test_profile_relationships_pandas_dispatcher(rel_df):
    rel = profile_relationships(rel_df, engine="pandas")
    assert rel["row_count"] == 4


def test_apply_relationship_flags_on_profile_result(rel_df):
    rel = column_relationships(rel_df)
    profile = ColumnProfile(
        column="c", dtype="int64", cardinality_ratio=0.25,
        avg_value_length=0.0, null_rate=0.0, name_hint_score=0.0,
        arabic_fraction=0.0, triage_score=0.1, send_to_detector=False,
    )
    result = ProfileResult(
        column_profiles=[profile],
        arabic_columns={},
        triage_signals=[profile],
        suggested_rules=QualityRuleSet(),
    )
    apply_relationship_flags(result, rel)
    assert result.raw["relationships"] is rel
    assert profile.constant is True
    assert profile.triage_score >= 0.85


def test_resolve_relationship_data_pandas_when_no_spark(rel_df):
    data, backend = resolve_relationship_data(
        rel_df, engine="auto", source=SourceConfig(engine="hive"), table="db.t",
    )
    assert backend == "pandas"
    assert data is rel_df


@patch("redibis.services.pipeline.get_profiler")
def test_profile_dataframe_relationships_pandas_sample(mock_get, rel_df):
    fake = ProfileResult(
        column_profiles=[],
        arabic_columns={},
        triage_signals=[],
        suggested_rules=QualityRuleSet(),
    )
    mock_profiler = MagicMock()
    mock_profiler.profile.return_value = fake
    mock_get.return_value = mock_profiler

    out = pipeline.profile_dataframe(rel_df, "tbl", relationships=True)
    assert out.raw["relationships"]["row_count"] == 4
    assert out.raw["relationships"]["backend"] == "pandas"


@patch("redibis.services.pipeline.get_profiler")
def test_profile_dataframe_relationships_spark_table(mock_get, rel_df):
    pytest.importorskip("pyspark")
    from pyspark.sql import SparkSession

    try:
        spark = (
            SparkSession.builder
            .master("local[1]")
            .appName("redibis_relationships_test")
            .config("spark.ui.enabled", "false")
            .getOrCreate()
        )
    except Exception as exc:
        pytest.skip(f"Spark not available: {exc}")

    spark.createDataFrame(rel_df).createOrReplaceTempView("rel_sample")

    fake = ProfileResult(
        column_profiles=[],
        arabic_columns={},
        triage_signals=[],
        suggested_rules=QualityRuleSet(),
    )
    mock_profiler = MagicMock()
    mock_profiler.profile.return_value = fake
    mock_get.return_value = mock_profiler

    try:
        out = pipeline.profile_dataframe(
            rel_df.head(2),
            "rel_sample",
            relationships=True,
            source=SourceConfig(engine="hive"),
            table="rel_sample",
            spark=spark,
        )
        assert out.raw["relationships"]["backend"] == "spark"
        assert out.raw["relationships"]["row_count"] == 4
    finally:
        spark.stop()


@pytest.mark.slow
def test_column_relationships_spark_list_methods_fast_path(rel_df):
    pytest.importorskip("pyspark")
    from pyspark.sql import SparkSession
    from redibis.profiling.relationships_spark import column_relationships_spark

    try:
        spark = (
            SparkSession.builder
            .master("local[1]")
            .appName("redibis_relationships_spark_test")
            .config("spark.ui.enabled", "false")
            .getOrCreate()
        )
    except Exception as exc:
        pytest.skip(f"Spark not available: {exc}")

    sdf = spark.createDataFrame(rel_df)
    try:
        rel = column_relationships_spark(sdf, methods=["pearson"])
        assert "pearson" in rel["correlations"]
        assert rel["row_count"] == 4
    finally:
        spark.stop()
