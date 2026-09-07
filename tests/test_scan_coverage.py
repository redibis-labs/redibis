"""ScanCoverage artifact — build, write, load, and reconcile gate (C2)."""

from __future__ import annotations

from pathlib import Path

from redibis.models import PIIDetection
from redibis.scan.report_bundle import ReportBundle
from redibis.scan.types import ScanRunResult
from redibis.services.catalog.assertions import (
    CoverageStatus,
    Facet,
)
from redibis.services.catalog.reconcile import reconcile
from redibis.services.pipeline import (
    SCAN_COVERAGE_ARTIFACT,
    build_scan_coverage,
    load_latest_scan_coverage,
    parse_scan_coverage_payload,
    scan_coverage_payload,
    write_scan_coverage,
)
from redibis.services.scan_service import ScanConfig
from redibis.store.storage_backend import LocalBackend
from redibis.store.run_output_writer import RunOutputWriter


def test_build_scan_coverage_skipped_by_triage():
    records = build_scan_coverage(
        scan_id="s1",
        columns=["msisdn", "blob_payload"],
        run_pii=True,
        pii_detections=[
            PIIDetection(
                column="msisdn",
                detected=True,
                decision_path="presidio>=0.8",
            ),
            PIIDetection(
                column="blob_payload",
                detected=False,
                decision_path="skipped_by_triage",
            ),
        ],
    )
    by_key = {(r.column_path, r.facet): r for r in records}
    assert by_key[("msisdn", Facet.PII_TAG)].status is CoverageStatus.EVALUATED
    skipped = by_key[("blob_payload", Facet.PII_TAG)]
    assert skipped.status is CoverageStatus.SKIPPED
    assert "triage" in skipped.reason


def test_build_scan_coverage_column_excluded():
    records = build_scan_coverage(
        scan_id="s1",
        columns=["a", "b"],
        run_pii=True,
        pii_column_filter=["a"],
        pii_detections=[
            PIIDetection(column="a", detected=False, decision_path="ok"),
        ],
    )
    by_col = {r.column_path: r for r in records if r.facet is Facet.PII_TAG}
    assert by_col["a"].status is CoverageStatus.EVALUATED
    assert by_col["b"].status is CoverageStatus.SKIPPED
    assert by_col["b"].reason == "column excluded"


def test_build_scan_coverage_profiler_error():
    records = build_scan_coverage(
        scan_id="s1",
        columns=["x"],
        run_profile=True,
        profile_error="profiler boom",
    )
    assert any(
        r.facet is Facet.TABLE_DESCRIPTION and r.status is CoverageStatus.ERROR
        for r in records
    )
    assert any(
        r.column_path == "x"
        and r.facet is Facet.COLUMN_DESCRIPTION
        and r.status is CoverageStatus.ERROR
        for r in records
    )


def test_write_and_load_scan_coverage(tmp_path: Path):
    backend = LocalBackend(str(tmp_path))
    writer = RunOutputWriter(
        backend=backend,
        bucket="runs",
        workflow="scan",
        table="telecom.customers",
        run_id="2026-08-03_12-00-00",
    )
    records = build_scan_coverage(
        scan_id="2026-08-03_12-00-00",
        columns=["msisdn"],
        run_pii=True,
        pii_detections=[
            PIIDetection(
                column="msisdn",
                detected=False,
                decision_path="skipped_by_triage",
            ),
        ],
    )
    key = write_scan_coverage(
        writer, records, table="telecom.customers", scan_id="2026-08-03_12-00-00",
    )
    assert key.endswith(SCAN_COVERAGE_ARTIFACT)
    assert backend.exists("runs", key)

    loaded = load_latest_scan_coverage(backend, "runs", "telecom.customers")
    assert loaded is not None
    assert len(loaded) == 3  # pii/entity/policy facets
    assert all(r.status is CoverageStatus.SKIPPED for r in loaded)


def test_report_bundle_writes_scan_coverage(tmp_path: Path):
    run_dir = tmp_path / "artifacts"
    config = ScanConfig(table="telecom.customers", run_pii=True, run_profile=False)
    result = ScanRunResult(
        run_id="run_cov",
        table="telecom.customers",
        col_dtypes={"msisdn": "string", "notes": "string"},
        pii_detections=[
            PIIDetection(column="msisdn", detected=True, decision_path="ok"),
            PIIDetection(
                column="notes", detected=False, decision_path="skipped_by_triage",
            ),
        ],
    )
    artifacts = ReportBundle(result, config).flush(run_dir)
    assert "scan_coverage" in artifacts
    payload = parse_scan_coverage_payload(
        __import__("json").loads(
            Path(artifacts["scan_coverage"]).read_text(encoding="utf-8")
        )
    )
    notes = [r for r in payload if r.column_path == "notes" and r.facet is Facet.PII_TAG]
    assert notes and notes[0].status is CoverageStatus.SKIPPED


def test_skipped_coverage_blocks_deletion_via_payload():
    """End-to-end: SKIPPED coverage from artifact → reconcile emits no ops."""
    from redibis.services.catalog.assertions import (
        Assertion,
        AssertionKey,
        Authority,
        CoverageRecord,
        LedgerEntry,
        value_hash,
    )
    from datetime import datetime, timezone

    asset = "svc.db.schema.t"
    key = AssertionKey(
        asset_fqn=asset,
        column_path="blob_payload",
        facet=Facet.PII_TAG,
        value_key="PII.PHONE_NUMBER",
    )
    payload = scan_coverage_payload(
        [
            CoverageRecord(
                scan_id="s1",
                asset_fqn="",
                column_path="blob_payload",
                facet=Facet.PII_TAG,
                status=CoverageStatus.SKIPPED,
                reason="skipped by triage",
            )
        ],
        table="db.t",
        scan_id="s1",
    )
    coverage = parse_scan_coverage_payload(payload)
    from redibis.services.catalog.assertions import rebind_coverage_asset_fqn

    coverage = rebind_coverage_asset_fqn(coverage, asset)
    now = datetime(2026, 8, 3, tzinfo=timezone.utc)
    plan = reconcile(
        backend="openmetadata",
        asset_fqn=asset,
        desired=[],
        remote=[
            Assertion(
                key=key,
                value="PII.PHONE_NUMBER",
                authority=Authority.REDIBIS,
            )
        ],
        ledger=[
            LedgerEntry(
                backend="openmetadata",
                key=key,
                value_hash=value_hash("PII.PHONE_NUMBER"),
                scan_id="prev",
                published_at=now,
            )
        ],
        coverage=coverage,
        suppressions=[],
        now=now,
    )
    assert plan.ops == []
    assert plan.skipped_uncovered == [key]
