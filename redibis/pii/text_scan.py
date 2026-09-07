"""Design-doc façade module — re-exports the unified text-scan surface.

``TEXT_PII_SCAN_DESIGN.md`` names this module ``redibis.pii.text_scan``. The
implementation lives under ``redibis.pii.scan`` (shared RuleSet scanners); this
module keeps the documented import path stable.
"""

from redibis.pii.deid import DeidApplier, DeidPolicy, DeidResult, EntityRule
from redibis.pii.scan.result import (
    OFFSET_UNIT,
    Detection,
    DetectionResult,
    PIISpan,
    TextScanConfig,
    TextScanResult,
)
from redibis.pii.scan.text_scanner import TextPIIScan, TextScanner

__all__ = [
    "OFFSET_UNIT",
    "DeidApplier",
    "DeidPolicy",
    "DeidResult",
    "Detection",
    "DetectionResult",
    "EntityRule",
    "PIISpan",
    "TextPIIScan",
    "TextScanConfig",
    "TextScanResult",
    "TextScanner",
]
