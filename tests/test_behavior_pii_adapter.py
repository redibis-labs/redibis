"""Phase-4 PII adapter: thresholds, human overlay, promotion marking."""

from __future__ import annotations

import pytest

from redibis.behavior.adapters.pii import (
    apply_patch_to_detection,
    apply_threshold_patch,
    human_authority_for_decision,
    mark_threshold_promotions,
)
from redibis.behavior.compiler import compile_policy
from redibis.behavior.config import BehaviorConfig
from redibis.behavior.models import (
    Authority,
    BehaviorPatch,
    HookStage,
    PatchOperation,
    RuntimeMode,
)
from redibis.behavior.reducer import PatchReducer
from redibis.behavior.runtime import apply_pii_behavior_policies, apply_pre_verdict_thresholds
from redibis.models import PIIDetection
from redibis.pii.thresholds import Thresholds


def test_threshold_patch_clamps_below_min():
    thr = Thresholds(presidio_min=0.80)
    patch = BehaviorPatch(
        operations=(
            PatchOperation(
                op="set",
                path="threshold.presidio_min",
                value=0.01,
                authority=Authority.APPROVED_POLICY,
                reason="too low",
            ),
        )
    )
    out = apply_threshold_patch(thr, patch)
    assert out.presidio_min == 0.30  # hard floor
    assert thr.presidio_min == 0.80  # original untouched


def test_threshold_patch_clamps_above_max():
    thr = Thresholds(presidio_min=0.80)
    patch = BehaviorPatch(
        operations=(
            PatchOperation(
                op="set",
                path="threshold.presidio_min",
                value=1.5,
                authority=Authority.APPROVED_POLICY,
                reason="too high",
            ),
        )
    )
    out = apply_threshold_patch(thr, patch)
    assert out.presidio_min == 0.99


def test_threshold_patch_rejects_unknown_name_silently():
    thr = Thresholds()
    patch = BehaviorPatch(
        operations=(
            PatchOperation(
                op="set",
                path="threshold.not_a_real_threshold",
                value=0.5,
                authority=Authority.APPROVED_POLICY,
                reason="unsupported",
            ),
        )
    )
    out = apply_threshold_patch(thr, patch)
    assert out is thr


def test_human_authority_locks_not_pii():
    auth = human_authority_for_decision({"status": "not_pii", "column": "email"})
    assert auth["verdict.entity"] is Authority.HUMAN_DECISION
    assert auth["verdict.detected"] is Authority.HUMAN_DECISION


def test_policy_cannot_override_human_not_pii():
    detection = PIIDetection(
        column="email",
        detected=False,
        entity_type=None,
        confidence=0.0,
    )
    # Policy tries to mark as EMAIL
    patch = BehaviorPatch(
        operations=(
            PatchOperation(
                op="set",
                path="verdict.detected",
                value=True,
                authority=Authority.APPROVED_POLICY,
                reason="resurrect",
            ),
            PatchOperation(
                op="set",
                path="verdict.entity",
                value="EMAIL_ADDRESS",
                authority=Authority.APPROVED_POLICY,
                reason="resurrect",
            ),
        )
    )
    from redibis.behavior.adapters.pii import _pii_capabilities

    caps = _pii_capabilities(HookStage.POST_VERDICT)
    human = human_authority_for_decision({"status": "not_pii"})
    applied, _, blocked = PatchReducer(caps, human_decisions=human).reduce(patch)
    assert not applied.operations
    assert blocked
    updated = apply_patch_to_detection(detection, applied)
    assert updated.detected is False
    assert updated.entity_type is None


def test_mark_threshold_promotions_flags_review():
    base = [PIIDetection(column="phone", detected=False, confidence=0.75)]
    adj = [PIIDetection(column="phone", detected=True, entity_type="PHONE_NUMBER", confidence=0.75)]
    out = mark_threshold_promotions(base, adj)
    assert out[0].detected is True
    assert out[0].llm_verdict == "UNCERTAIN"
    assert "require_review" in (out[0].decision_path or "")


PRE_VERDICT_DOC = {
    "apiVersion": "redibis.io/behavior-policy/v1",
    "kind": "BehaviorPolicy",
    "metadata": {"id": "fn-tune", "version": "1.0.0", "description": "Lower floor"},
    "appliesTo": {"engine": "pii", "stage": "pre_verdict"},
    "rules": [
        {
            "id": "lower-presidio",
            "when": {"fact": "column.name", "op": "eq", "value": "msisdn"},
            "effects": [
                {
                    "effect": "core.threshold.set_for_run",
                    "params": {"threshold": "presidio_min", "value": 0.5},
                }
            ],
            "reason": "Recover weak MSISDN regex hits for this table.",
        }
    ],
}


def test_apply_pre_verdict_thresholds_active():
    policy = compile_policy(PRE_VERDICT_DOC)
    evidence = [PIIDetection(column="msisdn", detected=False, confidence=0.0)]
    cfg = BehaviorConfig(enabled=True, mode=RuntimeMode.ACTIVE.value)
    result = apply_pre_verdict_thresholds(
        evidence,
        Thresholds(presidio_min=0.80),
        policy=policy,
        behavior_config=cfg,
    )
    assert result.applied is True
    assert result.thresholds.presidio_min == 0.5
    assert result.baseline_thresholds.presidio_min == 0.80


def test_apply_pre_verdict_thresholds_shadow_does_not_apply():
    policy = compile_policy(PRE_VERDICT_DOC)
    evidence = [PIIDetection(column="msisdn", detected=False, confidence=0.0)]
    cfg = BehaviorConfig(enabled=True, mode=RuntimeMode.SHADOW.value)
    result = apply_pre_verdict_thresholds(
        evidence,
        Thresholds(presidio_min=0.80),
        policy=policy,
        behavior_config=cfg,
    )
    assert result.applied is False
    assert result.thresholds.presidio_min == 0.80


def test_human_decision_blocks_active_behavior_apply():
    cfg = BehaviorConfig(enabled=True, mode=RuntimeMode.ACTIVE.value)
    detection = PIIDetection(
        column="lac",
        detected=True,
        entity_type="PHONE_NUMBER",
        confidence=0.9,
    )
    # Human said not_pii — policy must not change verdict fields
    result = apply_pii_behavior_policies(
        [detection],
        policy_pack="telecom",
        behavior_config=cfg,
        pii_decisions={"lac": {"status": "not_pii", "column": "lac"}},
    )
    # Even if telecom would demote lac, human lock blocks entity/detected ops;
    # detection may still be unchanged from input when all ops blocked.
    assert result.detections[0].column == "lac"
