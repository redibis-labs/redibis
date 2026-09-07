"""Scan **engine** package.

Layering (see ``docs/ARCHITECTURE_NAMING_REVIEW.md``):
- ``Scan`` -- the in-memory scan **engine** (template method); returns ``EngineScanResult``.
- ``ScanService`` (in ``redibis.services.scan_service``) -- the **service** that wraps the
  engine with file/S3/contract I/O; returns ``ScanResult``.
- ``ScanConfig`` (``redibis.scan.config``) -- the engine config both consume.
- Library entry: the facades ``PIIScan`` / ``QualityScan`` / ``ProfileScan``.
"""

from redibis.scan.base import Scan
from redibis.scan.config import ScanConfig
from redibis.scan.contract_writer import ScanContractWriter
from redibis.scan.facades import PIIScan, ProfileScan, QualityScan
from redibis.scan.pii_scanner import PIIScanner
from redibis.scan.report_bundle import ReportBundle
from redibis.scan.types import (
    ContractDraft,
    ContractPersistResult,
    EngineScanResult,
    ScanRunResult,  # back-compat alias of EngineScanResult
)

__all__ = [
    "Scan",
    "ScanConfig",
    "PIIScanner",
    "PIIScan",
    "ProfileScan",
    "QualityScan",
    "ReportBundle",
    "ContractDraft",
    "ScanContractWriter",
    "ContractPersistResult",
    "EngineScanResult",
    "ScanRunResult",
]
