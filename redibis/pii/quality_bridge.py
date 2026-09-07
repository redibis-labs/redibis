"""
redibis.pii.quality_bridge
==========================
PIIQualityBridge — registers PII detections as GE expectations on a
QualityGatekeeper, without the quality package needing to know about PII.

Usage
-----
    gatekeeper = QualityGatekeeper(...)
    gatekeeper.attach_dataframe(df, "telecom_customers")
    bridge = PIIQualityBridge(gatekeeper)
    for d in detections:
        bridge.register(d)
    gatekeeper.run_tests(generate_docs=True)
"""

from __future__ import annotations

from typing import List, Optional

from redibis.models import PIIDetection, RunMetadata


class PIIQualityBridge:
    """
    Registers PIIDetection results as GE regex expectations on a QualityGatekeeper.
    The bridge is ONE-WAY: writes INTO the gatekeeper; the gatekeeper never calls back.
    """

    def __init__(self, gatekeeper) -> None:
        self.gatekeeper = gatekeeper
        self._registered: list[PIIDetection] = []
        self._run_metadata: Optional[RunMetadata] = None

    def set_run_metadata(self, metadata: RunMetadata) -> "PIIQualityBridge":
        self._run_metadata = metadata
        return self

    def register(self, detection: PIIDetection) -> "PIIQualityBridge":
        """
        Register a single PIIDetection. If detected and has a regex_pattern,
        adds a regex expectation to the GE suite.
        """
        self._registered.append(detection)
        if not detection.detected or not detection.regex_pattern:
            return self

        # Register as a GE regex expectation via the gatekeeper's generic API
        try:
            self.gatekeeper.add_gx_expectation(
                expectation_type="expect_column_values_to_match_regex",
                column=detection.column,
                regex=detection.regex_pattern,
                mostly=detection.suggested_mostly,
                meta={
                    "source": "pii_detection",
                    "entity_type": detection.entity_type,
                    "confidence": detection.confidence,
                    "regex_pattern": detection.presidio_pattern,
                    "regex_score": detection.presidio_score,
                    "ner_score": detection.gliner_score,
                    "arabic_aware": detection.arabic_aware,
                },
            )
        except (AttributeError, NotImplementedError):
            pass  # gatekeeper may not support add_gx_expectation yet

        return self

    def register_all(self, detections: List[PIIDetection]) -> "PIIQualityBridge":
        for d in detections:
            self.register(d)
        return self

    def get_registered_detections(self) -> List[PIIDetection]:
        return list(self._registered)

    def get_run_metadata(self) -> Optional[RunMetadata]:
        return self._run_metadata
