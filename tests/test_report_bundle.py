"""Tests for ReportBundle.flush (Phase 4)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import yaml

from redibis.scan.report_bundle import ReportBundle
from redibis.scan.types import ScanRunResult
from redibis.services.scan_service import ScanConfig


def test_report_bundle_flush_writes_contract_and_pii_artifacts(tmp_path):
    run_dir = tmp_path / "artifacts"
    config = ScanConfig(
        table="telecom.customers",
        run_quality=True,
        run_pii=True,
        equation_mode="independent",
    )
    result = ScanRunResult(
        run_id="run_1",
        table="telecom.customers",
        quality_contract={"apiVersion": "v3.0.1", "quality": []},
        pii_contract={"apiVersion": "v3.0.1", "schema": []},
        pii_detections=[],
    )
    profile = MagicMock()
    result.profile = profile

    artifacts = ReportBundle(result, config).flush(run_dir)

    assert (run_dir / "quality_contract.yaml").exists()
    assert (run_dir / "pii_contract.yaml").exists()
    assert "quality_contract" in artifacts
    assert "pii_contract" in artifacts
    profile.export_interactive_review.assert_called_once()
    profile.export_triage_report.assert_called_once()

    loaded = yaml.safe_load((run_dir / "quality_contract.yaml").read_text(encoding="utf-8"))
    assert loaded["apiVersion"] == "v3.0.1"
    assert (run_dir / "evidence_bundle.json").exists()
    assert (run_dir / "evidence_manifest.json").exists()
    assert (run_dir / "effective_config.yaml").exists()
    assert (run_dir / "result_variants.json").exists()
