"""Effective verdict resolution — steward authority with fingerprint gating."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from redibis.review.drift import DriftResult, LIFECYCLE_ACTIVE, LIFECYCLE_STALE, evaluate_column_drift
from redibis.review.fingerprint import ColumnFingerprintSnapshot


@dataclass(frozen=True)
class EffectiveVerdict:
    detected: bool
    entity_type: Optional[str]
    confidence: Optional[float]
    source: str  # engine | steward | supplied | none
    authority_reason: str
    engine_proposal: dict[str, Any] = field(default_factory=dict)
    steward_decision: Optional[dict[str, Any]] = None
    supplied_decision: Optional[dict[str, Any]] = None
    drift: Optional[DriftResult] = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "detected": self.detected,
            "entity_type": self.entity_type,
            "confidence": self.confidence,
            "source": self.source,
            "authority_reason": self.authority_reason,
            "engine_proposal": dict(self.engine_proposal),
        }
        if self.steward_decision is not None:
            out["steward_decision"] = self.steward_decision
        if self.supplied_decision is not None:
            out["supplied_decision"] = self.supplied_decision
        if self.drift is not None:
            out["drift"] = {
                "state": self.drift.state,
                "reasons": list(self.drift.reasons),
                "fingerprint_match": self.drift.fingerprint_match,
                "is_authoritative": self.drift.is_authoritative,
            }
        return out


def _engine_from_bundle(col_block: dict) -> dict[str, Any]:
    verdict = (col_block or {}).get("pii_verdict") or {}
    return {
        "detected": bool(verdict.get("detected")),
        "entity_type": verdict.get("entity_type"),
        "confidence": verdict.get("confidence"),
        "equation_id": verdict.get("equation_id"),
        "deciding_engines": list(verdict.get("deciding_engines") or []),
    }


def _verdict_from_pii_decision(decision: dict) -> dict[str, Any]:
    status = str((decision or {}).get("status") or "")
    payload = (decision or {}).get("payload") or {}
    entity = (decision or {}).get("entity_type") or payload.get("entity_type")
    return {
        "detected": status == "pii",
        "entity_type": entity,
        "status": status,
        "decided_by": (decision or {}).get("decided_by", ""),
        "ts": (decision or {}).get("ts", ""),
        "reason": (decision or {}).get("reason", ""),
        "lifecycle_state": (decision or {}).get("lifecycle_state") or LIFECYCLE_ACTIVE,
        "fingerprint_key": (decision or {}).get("fingerprint_key", ""),
    }


def _baseline_from_decision(decision: dict) -> Optional[ColumnFingerprintSnapshot]:
    if not decision:
        return None
    if not any(decision.get(k) for k in ("fingerprint_key", "logical_type", "format_signature")):
        return None
    return ColumnFingerprintSnapshot.from_dict({
        "column": decision.get("column", ""),
        "name_normalized": decision.get("name_normalized", ""),
        "logical_type": decision.get("logical_type", "string"),
        "physical_type": decision.get("physical_type", "string"),
        "format_signature": decision.get("format_signature", "unknown"),
        "fingerprint_key": decision.get("fingerprint_key", ""),
    })


def resolve_effective_verdict(
    *,
    column: str,
    col_block: Optional[dict] = None,
    current_fingerprint: Optional[ColumnFingerprintSnapshot] = None,
    steward_decision: Optional[dict] = None,
    supplied_decision: Optional[dict] = None,
) -> EffectiveVerdict:
    """Precedence: active local steward > matching supplied > engine proposal."""
    engine = _engine_from_bundle(col_block or {})

    if steward_decision:
        baseline = _baseline_from_decision(steward_decision)
        drift = evaluate_column_drift(
            baseline,
            current_fingerprint,
            lifecycle_state=str(steward_decision.get("lifecycle_state") or LIFECYCLE_ACTIVE),
        )
        steward_view = _verdict_from_pii_decision(steward_decision)
        if drift.is_authoritative:
            return EffectiveVerdict(
                detected=bool(steward_view["detected"]),
                entity_type=steward_view.get("entity_type"),
                confidence=engine.get("confidence"),
                source="steward",
                authority_reason="active_steward_decision",
                engine_proposal=engine,
                steward_decision=steward_view,
                drift=drift,
            )

    if supplied_decision:
        baseline = _baseline_from_decision(supplied_decision)
        drift = evaluate_column_drift(baseline, current_fingerprint)
        supplied_view = _verdict_from_pii_decision(supplied_decision)
        if drift.fingerprint_match or drift.state == "legacy":
            return EffectiveVerdict(
                detected=bool(supplied_view["detected"]),
                entity_type=supplied_view.get("entity_type"),
                confidence=engine.get("confidence"),
                source="supplied",
                authority_reason="matching_supplied_verdict",
                engine_proposal=engine,
                supplied_decision=supplied_view,
                steward_decision=_verdict_from_pii_decision(steward_decision) if steward_decision else None,
                drift=drift,
            )

    drift = None
    if steward_decision:
        drift = evaluate_column_drift(
            _baseline_from_decision(steward_decision),
            current_fingerprint,
            lifecycle_state=str(steward_decision.get("lifecycle_state") or LIFECYCLE_STALE),
        )

    return EffectiveVerdict(
        detected=bool(engine.get("detected")),
        entity_type=engine.get("entity_type"),
        confidence=engine.get("confidence"),
        source="engine",
        authority_reason="engine_proposal",
        engine_proposal=engine,
        steward_decision=_verdict_from_pii_decision(steward_decision) if steward_decision else None,
        supplied_decision=_verdict_from_pii_decision(supplied_decision) if supplied_decision else None,
        drift=drift,
    )
