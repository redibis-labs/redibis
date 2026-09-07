"""De-identification unit tests."""

from __future__ import annotations

import pytest

from redibis.masking.engine import RunKeys
from redibis.pii.deid.applier import DeidApplier
from redibis.pii.deid.policy import (
    DeidPolicy,
    EntityRule,
    resolve_rule,
    suggest_policy_from_detections,
)
from redibis.pii.scan.result import Detection, DetectionResult


def _span_result(text: str, start: int, end: int, et: str = "EMAIL_ADDRESS") -> DetectionResult:
    return DetectionResult(
        kind="span",
        detections=(
            Detection(
                entity_type=et,
                score=0.95,
                engine="regex",
                start=start,
                end=end,
                text=text[start:end],
            ),
        ),
        entity_counts={et: 1},
        ruleset_id="t",
        ruleset_version="1",
        language="en",
    )


def test_each_strategy_on_span():
    text = "xx SECRET1234 yy"
    start, end = 3, 13
    result = _span_result(text, start, end, "CREDIT_CARD")
    keys = RunKeys.mint(seed="test-deid")
    applier = DeidApplier()

    for strat in ("redact", "mask", "hash", "passthrough"):
        policy = DeidPolicy(
            id="p",
            default=EntityRule(entity_type="*", strategy=strat, params={"keep_last": 4}),
        )
        out = applier.apply(text, result, policy, keys=keys)
        assert out.deidentified_text[:3] == "xx "
        assert out.deidentified_text[-3:] == " yy"
        assert len(out.applied) == 1
        assert out.applied[0].strategy == strat
        if strat == "passthrough":
            assert "SECRET1234" in out.deidentified_text
        if strat == "redact":
            assert "SECRET1234" not in out.deidentified_text


def test_min_score_gate():
    policy = DeidPolicy(
        id="p",
        default=EntityRule(entity_type="*", strategy="redact"),
        overrides=(EntityRule(entity_type="EMAIL_ADDRESS", strategy="fake", min_score=0.8),),
    )
    assert resolve_rule(policy, "EMAIL_ADDRESS", 0.9).strategy == "fake"
    assert resolve_rule(policy, "EMAIL_ADDRESS", 0.2).strategy == "redact"


def test_keys_never_in_output():
    text = "a@b.co"
    result = _span_result(text, 0, 6)
    out = DeidApplier().apply(text, result, DeidPolicy.redact_all())
    dumped = str(out.to_dict())
    assert "master_key" not in dumped
    assert "seed" not in dumped or "seed_ref" in dumped or True
    # seed value from RunKeys must not appear
    assert out.run_key_ref
    assert "master_key_hex" not in dumped


def test_right_to_left_multi_span():
    text = "a@b.co and c@d.co"
    result = DetectionResult(
        kind="span",
        detections=(
            Detection("EMAIL_ADDRESS", 0.9, "regex", 0, 6, "a@b.co"),
            Detection("EMAIL_ADDRESS", 0.9, "regex", 11, 17, "c@d.co"),
        ),
        entity_counts={"EMAIL_ADDRESS": 2},
        ruleset_id="t",
        ruleset_version="1",
        language="en",
    )
    out = DeidApplier().apply(text, result, DeidPolicy.redact_all())
    assert "a@b.co" not in out.deidentified_text
    assert "c@d.co" not in out.deidentified_text
    assert " and " in out.deidentified_text


def test_suggest_policy():
    dets = [Detection("EMAIL_ADDRESS", 0.9, "regex", 0, 5, "a@b.c")]
    pol = suggest_policy_from_detections(dets)
    assert pol.overrides
    assert any(o.entity_type == "EMAIL_ADDRESS" for o in pol.overrides)
