"""Tests for Scan orchestrator phase methods (Phase 4)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from redibis.profiling.base import ProfileResult
from redibis.quality.rule_set import QualityRuleSet
from redibis.scan.base import Scan
from redibis.scan.types import ScanRunResult
from redibis.services.scan_service import ScanConfig


@pytest.fixture
def sample_df():
    return pd.DataFrame({"phone": ["+201012345678"], "id": [1]})


@pytest.fixture
def scan_config():
    return ScanConfig(table="telecom.customers", run_quality=True, run_pii=False)


@pytest.fixture
def fake_profile():
    return ProfileResult(
        column_profiles=[],
        arabic_columns={},
        triage_signals=[],
        suggested_rules=QualityRuleSet(rules=[{"rule": "expect_column_values_to_not_be_null", "column": "phone", "kwargs": {}}]),
        raw={"ge_profiler": MagicMock(expectations=[])},
    )


def test_scan_profile_delegates_to_pipeline(sample_df, scan_config, fake_profile):
    with patch("redibis.scan.base.pipeline.profile_dataframe", return_value=fake_profile) as mock_profile:
        scan = Scan(scan_config)
        result = scan.profile(sample_df, tbl_name="customers")
        assert result is fake_profile
        mock_profile.assert_called_once()
        assert mock_profile.call_args.kwargs["profiling"].engine == "great_expectations"


def test_suggest_quality_rules_requires_profile(scan_config):
    scan = Scan(scan_config)
    with pytest.raises(RuntimeError, match="profile\\(\\)"):
        scan.suggest_quality_rules()


def test_suggest_quality_rules_returns_suggested_rules(scan_config, fake_profile):
    scan = Scan(scan_config)
    scan._profile = fake_profile
    rules = scan.suggest_quality_rules()
    assert rules.rules[0]["column"] == "phone"


@patch("redibis.scan.base.build_pii_contract", return_value={"apiVersion": "v3.0.1"})
@patch("redibis.scan.base.run_quality_phase")
@patch("redibis.scan.base.pipeline.profile_dataframe")
def test_scan_run_quality_only(
    mock_profile,
    mock_quality,
    mock_pii_contract,
    sample_df,
    fake_profile,
):
    mock_profile.return_value = fake_profile
    mock_quality.return_value = (
        {"apiVersion": "v3.0.1", "quality": []},
        MagicMock(),
        MagicMock(),
        {"total": 1, "passed": 1, "failed": 0},
    )
    config = ScanConfig(table="telecom.customers", run_profile=True, run_quality=True, run_pii=False)
    result = Scan(config).run(sample_df, run_id="run_test")

    assert isinstance(result, ScanRunResult)
    assert result.status == "success"
    assert result.quality_contract is not None
    assert result.pii_contract is None
    assert result.contract_draft is not None
    mock_pii_contract.assert_not_called()


@patch("redibis.scan.base.build_pii_contract", return_value={"apiVersion": "v3.0.1"})
@patch("redibis.scan.base.PIIScanner")
@patch("redibis.scan.base.pipeline.profile_dataframe")
def test_scan_run_pii_only(mock_profile, mock_scanner_cls, mock_pii_contract, sample_df, fake_profile):
    mock_profile.return_value = fake_profile
    scanner = MagicMock()
    scanner.detect.return_value = []
    mock_scanner_cls.return_value = scanner

    config = ScanConfig(table="telecom.customers", run_quality=False, run_pii=True)
    result = Scan(config).run(sample_df, run_id="run_pii")

    assert result.status == "success"
    assert result.pii_contract is not None
    scanner.detect.assert_called_once()
