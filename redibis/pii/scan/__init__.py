"""Unified PII scan surface — column + text scanners and shared result models."""

from redibis.pii.scan.column_scanner import ColumnScanner, pii_detections_to_result
from redibis.pii.scan.result import (
    OFFSET_UNIT,
    Candidate,
    Detection,
    DetectionResult,
    PIISpan,
    TextScanConfig,
    TextScanResult,
)
from redibis.pii.scan.text_scanner import TextPIIScan, TextScanner

__all__ = [
    "OFFSET_UNIT",
    "Candidate",
    "ColumnScanner",
    "Detection",
    "DetectionResult",
    "PIISpan",
    "TextPIIScan",
    "TextScanConfig",
    "TextScanResult",
    "TextScanner",
    "pii_detections_to_result",
]
