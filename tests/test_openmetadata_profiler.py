"""Tests for OpenMetadata profiler (Phase 5)."""

from __future__ import annotations

import pandas as pd
import pytest

from redibis.config import ProfilingConfig
from redibis.profiling import get_profiler, PROFILER_REGISTRY
from redibis.profiling.base import ProfileResult
from redibis.profiling.om_metrics import compute_column_metrics
from redibis.profiling.om_rules import metrics_to_rule_set
from redibis.profiling.openmetadata import OpenMetadataProfiler


@pytest.fixture
def sample_df():
    return pd.DataFrame({
        "id": [1, 2, 3, 4, 5],
        "status": ["active", "active", "inactive", "active", "active"],
        "score": [10.0, 20.0, 30.0, 40.0, 50.0],
    })


def test_openmetadata_registered():
    assert "open_metadata" in PROFILER_REGISTRY
    p = get_profiler(ProfilingConfig(engine="open_metadata"))
    assert isinstance(p, OpenMetadataProfiler)


def test_compute_column_metrics(sample_df):
    metrics = compute_column_metrics(sample_df)
    assert set(metrics.keys()) == {"id", "status", "score"}
    assert metrics["id"].unique_count == 5
    assert metrics["status"].unique_count == 2
    assert metrics["score"].min_value == 10.0
    assert metrics["score"].max_value == 50.0


def test_metrics_to_rule_set_suggests_rules(sample_df):
    metrics = compute_column_metrics(sample_df)
    rules = metrics_to_rule_set(metrics)
    rule_names = {r["rule"] for r in rules.rules}
    assert "expect_column_values_to_not_be_null" in rule_names
    assert "expect_column_values_to_be_between" in rule_names


def test_openmetadata_profiler_keeps_zip_as_string():
    df = pd.DataFrame({
        "zip_code": ["01234", "90210", "02101"],
        "quantity": [str(i) for i in range(1, 4)],
    })
    profiler = OpenMetadataProfiler(ProfilingConfig(engine="open_metadata"))
    result = profiler.profile(df, dataset_name="orders")

    by_col = {p.column: p for p in result.column_profiles}
    assert by_col["zip_code"].logical_type == "string"
    assert by_col["quantity"].logical_type == "integer"


def test_openmetadata_profiler_profile(sample_df):
    profiler = OpenMetadataProfiler(ProfilingConfig(engine="open_metadata"))
    result = profiler.profile(sample_df, dataset_name="test_table")

    assert isinstance(result, ProfileResult)
    assert result.native_report_html
    assert "test_table" in result.native_report_html
    assert result.suggested_rules.rules
    assert result.raw["engine"] == "open_metadata"
    assert "om_metrics" in result.raw
