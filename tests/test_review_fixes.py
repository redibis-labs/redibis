"""Regression tests for review feedback fixes."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from redibis.profiling.base import ProfileResult
from redibis.profiling.om_metrics import compute_column_metrics, metrics_to_type_hints
from redibis.profiling.om_rules import metrics_to_rule_set
from redibis.quality.rule_set import QualityRuleSet
from redibis.scan.base import Scan
from redibis.scan.facades import PIIScan
from redibis.scan.quality_phase import run_quality_phase
from redibis.services.scan_service import ScanConfig


def test_scan_error_handler_uses_module_logger():
    """Failed scans must not crash in except due to log param shadowing."""
    config = ScanConfig(table="db.t", run_profile=True, run_quality=False, run_pii=False)
    scan = Scan(config)

    with patch.object(scan, "profile", side_effect=RuntimeError("boom")):
        result = scan.run(pd.DataFrame({"a": [1]}), log_fn=lambda _m: None)

    assert result.status == "failed"
    assert result.error == "boom"


def test_quality_phase_falls_back_to_suggested_rules(tmp_path):
    profile = ProfileResult(
        column_profiles=[],
        arabic_columns={},
        triage_signals=[],
        suggested_rules=QualityRuleSet(rules=[{
            "rule": "expect_column_values_to_not_be_null",
            "column": "id",
            "kwargs": {"mostly": 0.99},
        }]),
        raw={"engine": "open_metadata"},
    )
    df = pd.DataFrame({"id": [1, 2, 3]})
    config = ScanConfig(table="db.t", generate_ge_docs=False)

    with patch("redibis.scan.quality_phase.validator_for_scan") as mock_factory:
        validator = MagicMock()
        mock_factory.return_value = validator
        validator.gatekeeper = MagicMock()
        validator.run_tests.return_value = MagicMock(run_results={})
        validator.export_quality_contract.return_value = {"quality": []}

        run_quality_phase(
            df, config, profile,
            db_name="db", tbl_name="t",
            run_dir=tmp_path,
            rule_set=None,
        )

        validator.apply_rules.assert_called_once()
        passed_rules = validator.apply_rules.call_args[0][0]
        assert passed_rules.rules == []
        profile_arg = validator.apply_rules.call_args[0][1]
        assert profile_arg.suggested_rules.rules[0]["column"] == "id"


def test_in_set_rule_not_emitted_when_frequent_values_truncated():
    values = [f"v{i}" for i in range(30)]
    df = pd.DataFrame({"cat": values})
    metrics = compute_column_metrics(df, max_frequent_values=10)
    rules = metrics_to_rule_set(metrics)
    in_set = [r for r in rules.rules if r["rule"] == "expect_column_values_to_be_in_set"]
    assert in_set == []


def test_in_set_rule_emitted_when_all_distinct_values_captured():
    df = pd.DataFrame({"status": ["a", "b", "a", "b", "a"]})
    metrics = compute_column_metrics(df, max_frequent_values=10)
    rules = metrics_to_rule_set(metrics)
    in_set = [r for r in rules.rules if r["rule"] == "expect_column_values_to_be_in_set"]
    assert len(in_set) == 1
    assert set(in_set[0]["kwargs"]["value_set"]) == {"a", "b"}


def test_coerced_integer_object_column_gets_integer_type():
    """Non-identifier columns with clean numeric strings may promote to integer."""
    df = pd.DataFrame({"quantity": [str(i) for i in range(1, 101)]})
    metrics = compute_column_metrics(df)
    hints = metrics_to_type_hints(metrics)
    assert hints["quantity"]["logical_type"] == "integer"
    assert metrics["quantity"].coerced_integer is True


def test_identifier_column_name_stays_string_despite_numeric_content():
    df = pd.DataFrame({"customer_id": [str(i) for i in range(1, 101)]})
    metrics = compute_column_metrics(df)
    hints = metrics_to_type_hints(metrics)
    assert hints["customer_id"]["logical_type"] == "string"
    assert metrics["customer_id"].coerced_integer is False


def test_leading_zero_zip_stays_string():
    df = pd.DataFrame({"zip_code": ["01234", "90210", "02101", "00501"]})
    metrics = compute_column_metrics(df)
    hints = metrics_to_type_hints(metrics)
    assert hints["zip_code"]["logical_type"] == "string"
    assert metrics["zip_code"].coerced_integer is False


def test_bare_zero_one_values_stay_string_not_boolean():
    df = pd.DataFrame({"active_flag": ["0", "1", "0", "1", "1", "0"]})
    metrics = compute_column_metrics(df)
    hints = metrics_to_type_hints(metrics)
    assert hints["active_flag"]["logical_type"] == "string"
    assert metrics["active_flag"].coerced_boolean is False


def test_pii_flagged_column_suppressed_at_type_hint_layer():
    df = pd.DataFrame({"phone": ["5551234567", "5559876543", "5550001111"]})
    metrics = compute_column_metrics(df)
    hints = metrics_to_type_hints(metrics, suppress_coercion_for={"phone"})
    assert hints["phone"]["logical_type"] == "string"


def test_coerced_iso_date_object_column_gets_date_type():
    df = pd.DataFrame({"d": ["2020-01-01", "2020-02-01", "2020-03-01"]})
    metrics = compute_column_metrics(df)
    hints = metrics_to_type_hints(metrics)
    assert hints["d"]["logical_type"] == "date"


def test_free_text_object_column_stays_string():
    df = pd.DataFrame({"note": ["hello world", "foo bar", "lorem ipsum"]})
    metrics = compute_column_metrics(df)
    hints = metrics_to_type_hints(metrics)
    assert hints["note"]["logical_type"] == "string"


def test_coerced_numeric_object_column_gets_number_type():
    df = pd.DataFrame({"amount": ["1.5", "2.0", "3.5", None]})
    metrics = compute_column_metrics(df)
    hints = metrics_to_type_hints(metrics)
    assert hints["amount"]["logical_type"] == "number"
    assert metrics["amount"].coerced_numeric is True


@patch("redibis.scan.facades.Scan.run")
def test_pii_scan_report_html_without_flush(mock_run):
    from redibis.models import PIIDetection
    from redibis.scan.types import ScanRunResult

    mock_run.return_value = ScanRunResult(
        run_id="r1",
        table="db.t",
        status="success",
        pii_detections=[
            PIIDetection(column="phone", detected=True, entity_type="PHONE_NUMBER"),
        ],
        pii_contract={"apiVersion": "v3.0.1"},
    )
    result = PIIScan(ScanConfig(table="db.t")).run(
        pd.DataFrame({"phone": ["x"]}), flush=False,
    )
    assert result.report_html is not None
    assert "PII Detection Report" in result.report_html
