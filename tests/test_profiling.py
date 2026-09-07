"""Tests for the profiling strategy seam (Phase 2)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from redibis.config import ConfigError, ProfilingConfig
from redibis.profiling import get_profiler, PROFILER_REGISTRY
from redibis.profiling.base import ProfileResult
from redibis.profiling.rules import expectations_to_rule_set
from redibis.services import pipeline


@pytest.fixture
def sample_df():
    return pd.DataFrame({
        "phone": ["+201012345678", "+201098765432"],
        "notes": ["hello", "مرحبا"],
        "id": [1, 2],
    })


def test_profiler_registry_default_engine():
    assert "great_expectations" in PROFILER_REGISTRY
    p = get_profiler(ProfilingConfig())
    assert p.name == "great_expectations"


def test_unknown_profiler_engine_raises():
    with pytest.raises(ConfigError, match="unknown profiler engine"):
        get_profiler(ProfilingConfig(engine="not_a_real_engine"))


def test_expectations_to_rule_set():
    exp = MagicMock()
    exp.expectation_type = "expect_column_values_to_not_be_null"
    exp.kwargs = {"column": "phone", "mostly": 0.95}
    exp.meta = {"notes": {"content": "mostly not null"}}
    rs = expectations_to_rule_set([exp])
    assert len(rs.rules) == 1
    assert rs.rules[0]["rule"] == "expect_column_values_to_not_be_null"
    assert rs.rules[0]["column"] == "phone"
    assert rs.rules[0]["kwargs"]["mostly"] == 0.95
    assert rs.rules[0]["meta"]["notes"]["content"] == "mostly not null"


@patch("redibis.quality.profiler.QualityProfiler")
def test_great_expectations_profiler_returns_profile_result(mock_qp, sample_df):
    ge = MagicMock()
    ge.expectations = [MagicMock(
        expectation_type="expect_column_values_to_not_be_null",
        kwargs={"column": "phone"},
    )]
    ge.arabic_columns = {"notes": 0.5}
    ge.compute_column_profiles.return_value = []
    mock_qp.return_value = ge

    from redibis.profiling.great_expectations import GreatExpectationsProfiler
    result = GreatExpectationsProfiler(ProfilingConfig()).profile(
        sample_df, dataset_name="test_table",
    )

    assert isinstance(result, ProfileResult)
    assert result.suggested_rules.rules
    assert result.raw["engine"] == "great_expectations"
    ge.run_assistant.assert_called_once()
    ge.profile_arabic_presence.assert_called_once()
    ge.compute_column_profiles.assert_called_once()


@patch("redibis.services.pipeline.get_profiler")
def test_profile_dataframe_returns_profile_result(mock_get, sample_df):
    fake = ProfileResult(
        column_profiles=[],
        arabic_columns={},
        triage_signals=[],
        suggested_rules=__import__(
            "redibis.quality.rule_set", fromlist=["QualityRuleSet"]
        ).QualityRuleSet(),
    )
    mock_profiler = MagicMock()
    mock_profiler.profile.return_value = fake
    mock_get.return_value = mock_profiler

    out = pipeline.profile_dataframe(sample_df, "tbl", triage_threshold=0.2)
    assert out is fake
    mock_get.assert_called_once()
    cfg = mock_get.call_args[0][0]
    assert cfg.triage_threshold == 0.2


def test_profile_result_compat_shim_delegates_exports():
    ge = MagicMock()
    ge.expectations = ["exp1"]
    result = ProfileResult(
        column_profiles=[],
        arabic_columns={"c": 0.1},
        triage_signals=[],
        suggested_rules=__import__(
            "redibis.quality.rule_set", fromlist=["QualityRuleSet"]
        ).QualityRuleSet(),
        raw={"ge_profiler": ge},
    )
    assert result.expectations == ["exp1"]
    assert result.triage_signals == []
    assert result.arabic_columns == {"c": 0.1}
    result.export_interactive_review(output_filename="/tmp/x.html", open_browser=False)
    ge.export_interactive_review.assert_called_once_with(
        output_filename="/tmp/x.html", open_browser=False,
    )
    result.export_triage_report(output_filename="/tmp/t.html")
    ge.export_triage_report.assert_called_once()


def test_profile_dataframe_integration_ge(sample_df):
    pytest.importorskip("great_expectations")
    result = pipeline.profile_dataframe(sample_df, "integration_tbl", triage_threshold=0.0)
    assert isinstance(result, ProfileResult)
    assert isinstance(result.expectations, list)
    assert len(result.triage_signals) >= 1
