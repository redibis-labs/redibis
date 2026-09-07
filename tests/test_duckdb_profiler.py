"""Tests for DuckDB SUMMARIZE profiler."""

from __future__ import annotations

import pandas as pd
import pytest

from redibis.config import ProfilingConfig
from redibis.profiling import PROFILER_REGISTRY, get_profiler
from redibis.profiling.base import ProfileResult
from redibis.profiling.duckdb_profiler import DuckDbProfiler


@pytest.fixture
def sample_df():
    return pd.DataFrame({
        "id": [1, 2, 3, 4, 5],
        "status": ["active", "active", "inactive", "active", "active"],
        "score": [10.0, 20.0, 30.0, 40.0, 50.0],
    })


def test_duckdb_registered():
    assert "duckdb" in PROFILER_REGISTRY
    profiler = get_profiler(ProfilingConfig(engine="duckdb"))
    assert isinstance(profiler, DuckDbProfiler)


def test_duckdb_profiler_profiles_three_columns(sample_df):
    profiler = DuckDbProfiler(ProfilingConfig(engine="duckdb"))
    result = profiler.profile(sample_df, dataset_name="test_table")

    assert isinstance(result, ProfileResult)
    assert result.raw["engine"] == "duckdb"
    assert len(result.column_profiles) == 3
    assert {p.column for p in result.column_profiles} == {"id", "status", "score"}

    by_col = {p.column: p for p in result.column_profiles}
    assert by_col["id"].null_rate == 0.0
    assert by_col["id"].cardinality_ratio == 1.0
    assert by_col["status"].cardinality_ratio == pytest.approx(0.4, abs=0.01)
    assert by_col["score"].dtype.upper().startswith("DOUBLE")
    assert by_col["id"].type_source == "duckdb"
    assert "summarize" in result.raw
    assert len(result.raw["summarize"]) == 3
