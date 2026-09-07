"""Structured decision channel — verdict explainability without raw values."""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

_DECISION_LOGGER = logging.getLogger("redibis.decision")
_decision_log_enabled = True

# Keys that must never appear — they imply raw cell payloads.
_BLOCKED_INPUT_KEYS = frozenset({
    "value",
    "values",
    "sample",
    "samples",
    "example",
    "examples",
    "cell",
    "cells",
    "raw",
    "text",
    "partial_unexpected",
    "observed_value",
})

# Pattern metadata — exempt from the value-like heuristic (not cell data).
_PATTERN_METADATA_KEYS = frozenset({
    "regex",
    "pattern",
    "pattern_name",
    "label",
    "labels",
    "match_count",
    "collision_group",
    "validator",
    "group",
})

# Keys allowed in decision ``inputs`` — scores/thresholds/ids/pattern metadata.
_ALLOWED_INPUT_KEYS = _PATTERN_METADATA_KEYS | frozenset({
    "match_rate",
    "score",
    "threshold",
    "engine",
    "nunique",
    "cardinality_ratio",
    "triage_score",
    "equation",
    "strategy",
    "expectation_type",
    "passed",
    "failed",
    "engines_agreeing",
    "profiler_contradicts",
    "entity_type",
    "rule_id",
    "presidio_score",
    "gliner_score",
    "llm_score",
    "presidio_min",
    "gliner_min",
    "llm_min",
    "pack_layers",
})

# Heuristic for non-pattern strings: emails, phones, long free text.
_VALUE_LIKE_RE = re.compile(
    r"(@|\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b|.{80,})"
)


@dataclass
class DecisionRecord:
    stage: str
    fn: str
    table: str
    column: str
    verdict: str
    confidence: float
    rule: str
    inputs: dict[str, Any] = field(default_factory=dict)
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def set_decision_log_enabled(enabled: bool) -> None:
    """Toggle decision channel emission (wired from ``ObservabilityConfig.decision_log``)."""
    global _decision_log_enabled
    _decision_log_enabled = enabled


def decision_log_enabled() -> bool:
    return _decision_log_enabled


def _is_value_like(key: str, val: str) -> bool:
    """Return True when a string looks like a cell payload, not pattern metadata."""
    if key in _PATTERN_METADATA_KEYS:
        return False
    return bool(_VALUE_LIKE_RE.search(val))


def sanitize_text(text: str) -> str:
    """Redact value-like content from free-text rule/verdict fields."""
    if not text:
        return ""
    if _VALUE_LIKE_RE.search(text):
        return "[redacted]"
    return text


def sanitize_inputs(inputs: dict[str, Any]) -> dict[str, Any]:
    """Strip disallowed keys and value-like strings from decision inputs."""
    clean: dict[str, Any] = {}
    for key, val in (inputs or {}).items():
        key_lower = key.lower()
        if key_lower in _BLOCKED_INPUT_KEYS:
            continue
        if key not in _ALLOWED_INPUT_KEYS:
            continue
        if isinstance(val, str) and _is_value_like(key, val):
            continue
        if isinstance(val, (list, tuple)):
            filtered = [
                v for v in val
                if not (isinstance(v, str) and _is_value_like(key, v))
            ]
            if filtered:
                clean[key] = filtered
            continue
        clean[key] = val
    return clean


def decision(rec: DecisionRecord) -> None:
    """Log a structured verdict to ``redibis.decision`` at INFO when enabled."""
    if not _decision_log_enabled:
        return
    safe = DecisionRecord(
        stage=rec.stage,
        fn=rec.fn,
        table=rec.table,
        column=rec.column,
        verdict=sanitize_text(rec.verdict),
        confidence=rec.confidence,
        rule=sanitize_text(rec.rule),
        inputs=sanitize_inputs(rec.inputs),
        ts=rec.ts,
    )
    _DECISION_LOGGER.info(
        "DECISION %s %s column=%s verdict=%s",
        safe.stage,
        safe.fn,
        safe.column or "-",
        safe.verdict,
        extra={"decision": asdict(safe)},
    )
