"""
Shared datatypes for the scan orchestrator.

Result-type map (canonical per caller)
--------------------------------------
``EngineScanResult``  — in-memory output of ``Scan.run()`` (engine; alias: ``ScanRunResult``)
``ScanResult``        — persisted service result from ``ScanService`` / CLI (twin of the above)
``ProfileScanResult`` — ``ProfileScan`` facade (profile-only)
``QualityScanResult`` — ``QualityScan`` facade (profile + quality)
``PIIScanResult``     — ``PIIScan`` facade (PII-only)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from redibis.models import PIIDetection
from redibis.profiling.base import ProfileResult
from redibis.services.scan_service import ScanResult


@dataclass
class ContractDraft:
    """Pure ODCS partials produced by a scan — no I/O."""

    table: str
    run_id: str
    schema_partial: Optional[dict] = None
    pii_partial: Optional[dict] = None
    quality_partial: Optional[dict] = None
    pii_summary: dict = field(default_factory=dict)
    quality_summary: dict = field(default_factory=dict)


@dataclass
class ContractPersistResult:
    schema_version: Optional[str] = None
    pii_version: Optional[str] = None
    quality_version: Optional[str] = None


@dataclass
class EngineScanResult:
    """In-memory **engine** result of ``Scan.run`` (pre report-flush / contract-persist).

    Service-level twin: ``ScanResult`` (in ``services.scan_service``). Back-compat alias
    ``ScanRunResult`` is defined just below. See ``docs/ARCHITECTURE_NAMING_REVIEW.md`` section 3.3.
    """

    run_id: str
    table: str
    session_id: Optional[str] = None
    status: str = "pending"
    error: Optional[str] = None

    total_rows: int = 0
    total_columns: int = 0
    quality_expectations: int = 0
    quality_passed: int = 0
    quality_failed: int = 0
    pii_columns_scanned: int = 0
    pii_columns_detected: int = 0
    arabic_aware_columns: int = 0

    quality_contract_version: Optional[str] = None
    pii_contract_version: Optional[str] = None

    profile: Optional[ProfileResult] = None
    pii_detections: list[PIIDetection] = field(default_factory=list)
    quality_contract: Optional[dict] = None
    pii_contract: Optional[dict] = None
    schema_contract: Optional[dict] = None
    contract_draft: Optional[ContractDraft] = None

    quality_gatekeeper: Any = None
    quality_results: Any = None
    col_dtypes: dict = field(default_factory=dict)
    classification: list = field(default_factory=list)

    artifacts: dict[str, str] = field(default_factory=dict)
    scan_log: list[str] = field(default_factory=list)
    detailed_log: list[str] = field(default_factory=list)

    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    duration_seconds: Optional[float] = None

    # Per-phase start/finish timestamps (``sampling``/``profiling``/``quality``/``pii``),
    # each ``{"started_at": iso, "finished_at": iso}``. Stamped by ``Scan.run()``;
    # ``duration_ms`` is computed downstream by the evidence bundle builder, not here.
    phase_timings: dict[str, dict] = field(default_factory=dict)
    evidence_warnings: list[dict] = field(default_factory=list)

    def to_scan_result(self) -> ScanResult:
        return ScanResult(
            run_id=self.run_id,
            table=self.table,
            session_id=self.session_id,
            status=self.status,
            error=self.error,
            total_rows=self.total_rows,
            total_columns=self.total_columns,
            quality_expectations=self.quality_expectations,
            quality_passed=self.quality_passed,
            quality_failed=self.quality_failed,
            pii_columns_scanned=self.pii_columns_scanned,
            pii_columns_detected=self.pii_columns_detected,
            arabic_aware_columns=self.arabic_aware_columns,
            quality_contract_version=self.quality_contract_version,
            pii_contract_version=self.pii_contract_version,
            artifacts=dict(self.artifacts),
            scan_log=list(self.scan_log),
            detailed_log=list(self.detailed_log),
            started_at=self.started_at,
            completed_at=self.completed_at,
            duration_seconds=self.duration_seconds,
            evidence_warnings=list(self.evidence_warnings),
        )


# Back-compat alias -- historical name. Prefer ``EngineScanResult`` in new code.
ScanRunResult = EngineScanResult
