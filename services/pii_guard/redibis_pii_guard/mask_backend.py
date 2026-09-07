"""Mask backend — library adapter over DeidApplier / DeidPolicy.

Must not import scan scanners or detector internals (split seam for a future
pii-mask service).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import pandas as pd

from redibis.masking.engine import RunKeys
from redibis.pii.deid.applier import DeidApplier, DeidResult
from redibis.pii.deid.policy import DeidPolicy
from redibis.pii.scan.result import Detection, DetectionResult

logger = logging.getLogger("pii_guard.mask")


class MaskBackend:
    """Service-side de-id adapter. Owns per-run keys; never returns key material."""

    def __init__(self, policies: Optional[dict[str, DeidPolicy]] = None):
        self._policies = dict(policies or {})
        self._applier = DeidApplier()

    def replace_policies(self, policies: dict[str, DeidPolicy]) -> None:
        self._policies = dict(policies)

    def list_policies(self) -> list[dict[str, str]]:
        return [
            {"id": p.id, "version": p.version, "locale": p.locale}
            for p in self._policies.values()
        ]

    def resolve_policy(
        self,
        *,
        policy_id: Optional[str] = None,
        policy: Optional[dict[str, Any] | DeidPolicy] = None,
    ) -> DeidPolicy:
        if policy is not None:
            if isinstance(policy, DeidPolicy):
                return policy
            return DeidPolicy.from_dict(dict(policy))
        if policy_id:
            pol = self._policies.get(policy_id)
            if pol is None:
                raise ValueError(f"unknown policy_id: {policy_id}")
            return pol
        raise ValueError("de-identification requires a policy (fail-closed)")

    def deidentify_text(
        self,
        text: str,
        result: DetectionResult,
        policy: DeidPolicy,
        *,
        keys: Optional[RunKeys] = None,
    ) -> DeidResult:
        out = self._applier.apply(text, result, policy, keys=keys)
        logger.info(
            "pii_guard_deid kind=span applied=%s policy=%s reversible=%s",
            len(out.applied),
            out.policy_id,
            out.reversible_spans,
        )
        return out

    def deidentify_columns(
        self,
        records: dict[str, list[Any]] | list[dict[str, Any]] | pd.DataFrame,
        result: DetectionResult,
        policy: DeidPolicy,
        *,
        keys: Optional[RunKeys] = None,
        only_detected: bool = True,
    ) -> DeidResult:
        if isinstance(records, pd.DataFrame):
            df = records
        elif isinstance(records, dict):
            df = pd.DataFrame(records)
        else:
            df = pd.DataFrame(records)
        out = self._applier.apply(
            df, result, policy, keys=keys, only_detected=only_detected
        )
        logger.info(
            "pii_guard_deid kind=column applied=%s policy=%s reversible=%s",
            len(out.applied),
            out.policy_id,
            out.reversible_spans,
        )
        return out

    @staticmethod
    def detections_from_payload(
        items: list[dict[str, Any]],
        *,
        kind: str,
        ruleset_id: str = "request",
        ruleset_version: str = "1",
        language: str = "en",
    ) -> DetectionResult:
        """Rebuild a DetectionResult from a client-supplied detection list."""
        dets: list[Detection] = []
        counts: dict[str, int] = {}
        for item in items:
            et = str(item.get("entity_type") or "UNKNOWN")
            det = Detection(
                entity_type=et,
                score=float(item.get("score") or 0),
                engine=str(item.get("engine") or "regex"),
                start=item.get("start"),
                end=item.get("end"),
                text=item.get("text"),
                detected=bool(item.get("detected", True)),
                recognizer=str(item.get("recognizer") or ""),
                validator=str(item.get("validator") or ""),
                is_proposal=bool(item.get("is_proposal", False)),
            )
            dets.append(det)
            counts[et] = counts.get(et, 0) + 1
        return DetectionResult(
            kind=kind,  # type: ignore[arg-type]
            detections=tuple(dets),
            entity_counts=counts,
            ruleset_id=ruleset_id,
            ruleset_version=ruleset_version,
            language=language,
        )
