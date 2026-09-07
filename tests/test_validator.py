"""Tests for quality Validator ABC seam."""

from __future__ import annotations

from redibis.quality.validator import GreatExpectationsValidator, validator_for_scan
from redibis.services.scan_service import ScanConfig


def test_validator_for_scan_returns_ge_implementation(tmp_path):
    config = ScanConfig(table="db.t", generate_ge_docs=False)
    validator = validator_for_scan(config, tbl_name="t", run_dir=tmp_path)
    assert isinstance(validator, GreatExpectationsValidator)
