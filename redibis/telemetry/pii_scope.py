"""Structural PII scope detection for RAI residency checks."""

from __future__ import annotations

import re
from typing import Any, Optional

from redibis.classification.ensemble import _schema_properties
from redibis.models import PIIDetection

_PII_TAGS = frozenset({
    "pii",
    "subscriberpii",
    "cpni",
    "biometricdata",
    "regulated",
    "rawpersonaldata",
})

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.\w{2,}", re.IGNORECASE)
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\d\s().-]{8,}\d)(?!\d)")
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")


def column_has_pii_markers(prop: dict[str, Any]) -> bool:
    """True when a contract column carries PII classification signals."""
    from redibis.contracts.privacy import column_is_pii

    return column_is_pii(prop)


def contract_columns_with_pii(
    contract: Optional[dict[str, Any]],
    table: str,
) -> list[str]:
    if not contract:
        return []
    return [
        str(prop.get("name"))
        for prop in _schema_properties(contract, table)
        if prop.get("name") and column_has_pii_markers(prop)
    ]


def detections_contain_pii(detections: Optional[list[PIIDetection]]) -> bool:
    if not detections:
        return False
    return any(d.detected for d in detections)


def payload_may_contain_raw_pii(text: str) -> bool:
    """Fast outbound-payload heuristic when contract metadata is incomplete."""
    if not text:
        return False
    sample = text[:50000]
    if _EMAIL_RE.search(sample):
        return True
    if _PHONE_RE.search(sample):
        return True
    if _SSN_RE.search(sample):
        return True
    lowered = sample.lower()
    if "raw personal data" in lowered and "do not treat as raw pii" not in lowered:
        return True
    return False


def infer_contains_raw_pii(
    *,
    contract: Optional[dict[str, Any]] = None,
    table: str = "",
    user_prompt: str = "",
    detections: Optional[list[PIIDetection]] = None,
    attested_masked_external: bool = False,
) -> tuple[bool, list[str]]:
    """
    Derive whether an outbound model call may carry raw PII.

    When ``attested_masked_external`` is True the steward has confirmed that
    masked sample data was uploaded and contract PII metadata was redacted for
    the outbound prompt — structural PII markers on the contract are ignored
    for residency checks (payload heuristics still apply).

    Returns ``(contains_raw_pii, pii_columns)``.
    """
    columns = contract_columns_with_pii(contract, table) if contract and table else []
    if columns and not attested_masked_external:
        return True, columns
    if detections_contain_pii(detections):
        return True, []
    if payload_may_contain_raw_pii(user_prompt):
        return True, []
    return False, columns
