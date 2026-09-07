"""Tests for scan facades (Phase 6)."""

from __future__ import annotations

from unittest.mock import patch

import pandas as pd

from redibis.scan.facades import PIIScan, ProfileScan, QualityScan
from redibis.services.scan_service import ScanConfig


@patch("redibis.scan.facades.Scan.run")
def test_profile_scan_runs_profile_only(mock_run):
    from redibis.scan.types import ScanRunResult
    from redibis.profiling.base import ProfileResult
    from redibis.quality.rule_set import QualityRuleSet

    mock_run.return_value = ScanRunResult(
        run_id="r1",
        table="db.t",
        status="success",
        profile=ProfileResult(
            column_profiles=[],
            arabic_columns={},
            triage_signals=[],
            suggested_rules=QualityRuleSet(),
            native_report_html="<html>profile</html>",
        ),
    )
    df = pd.DataFrame({"a": [1]})
    result = ProfileScan(ScanConfig(table="db.t")).run(df, flush=False)

    assert result.status == "success"
    assert result.native_report_html == "<html>profile</html>"
    mock_run.assert_called_once()


@patch("redibis.scan.facades.Scan.run")
def test_quality_scan_invokes_scan(mock_run):
    from redibis.scan.types import ScanRunResult

    mock_run.return_value = ScanRunResult(
        run_id="r1", table="db.t", status="success",
        quality_expectations=3, quality_passed=2,
    )
    QualityScan(ScanConfig(table="db.t")).run(pd.DataFrame({"a": [1]}), flush=False)
    mock_run.assert_called_once()


@patch("redibis.scan.facades.Scan.run")
def test_pii_scan_collects_flagged_columns(mock_run):
    from redibis.models import PIIDetection
    from redibis.scan.types import ScanRunResult

    mock_run.return_value = ScanRunResult(
        run_id="r1",
        table="db.t",
        status="success",
        pii_detections=[
            PIIDetection(column="phone", detected=True, entity_type="PHONE_NUMBER"),
            PIIDetection(column="id", detected=False),
        ],
        pii_contract={"apiVersion": "v3.0.1"},
    )
    result = PIIScan(ScanConfig(table="db.t")).run(
        pd.DataFrame({"phone": ["x"], "id": [1]}), flush=False,
    )
    assert result.flagged_columns == ["phone"]
    assert result.contract_partial is not None
