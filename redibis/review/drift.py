"""Column-scoped schema/format drift for steward decisions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from redibis.review.fingerprint import ColumnFingerprintSnapshot


LIFECYCLE_ACTIVE = "active"
LIFECYCLE_STALE = "stale"
LIFECYCLE_SUPERSEDED = "superseded"


@dataclass(frozen=True)
class DriftResult:
    state: str  # active | stale | new | removed | legacy
    reasons: list[str] = field(default_factory=list)
    fingerprint_match: bool = False

    @property
    def is_authoritative(self) -> bool:
        return self.state in (LIFECYCLE_ACTIVE, "legacy")


def evaluate_column_drift(
    baseline: Optional[ColumnFingerprintSnapshot],
    current: Optional[ColumnFingerprintSnapshot],
    *,
    lifecycle_state: str = "",
) -> DriftResult:
    """Compare stored decision fingerprint to the current column snapshot."""
    state = (lifecycle_state or LIFECYCLE_ACTIVE).strip().lower()
    if state == LIFECYCLE_SUPERSEDED:
        return DriftResult(state=LIFECYCLE_SUPERSEDED, reasons=["superseded_by_newer_decision"])

    if baseline is None and current is None:
        return DriftResult(state="unknown", reasons=["no_fingerprint"])

    if baseline is None and current is not None:
        return DriftResult(state="legacy", reasons=["no_baseline_fingerprint"], fingerprint_match=True)

    if baseline is not None and current is None:
        return DriftResult(state="removed", reasons=["column_removed_from_schema"])

    if baseline.fingerprint_key == current.fingerprint_key:
        return DriftResult(state=LIFECYCLE_ACTIVE, fingerprint_match=True)

    reasons: list[str] = []
    if baseline.name_normalized != current.name_normalized:
        reasons.append("column_rename_or_normalization_change")
    if baseline.logical_type != current.logical_type:
        reasons.append("logical_type_changed")
    if baseline.physical_type != current.physical_type:
        reasons.append("physical_type_changed")
    if baseline.format_signature != current.format_signature:
        reasons.append("format_signature_changed")
    if not reasons:
        reasons.append("fingerprint_changed")
    return DriftResult(state=LIFECYCLE_STALE, reasons=reasons, fingerprint_match=False)
