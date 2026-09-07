"""Phase 2 — learned classifier consume seam + suggest_only enforcement."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from redibis.classification import (
    CandidateTag,
    ClassificationContext,
    PolicyEngine,
    get_builtin_pack,
)
from redibis.classification.ensemble import ClassifierEnsemble, candidates_from_llm_suggestion
from redibis.models import PIIDetection
from redibis.pii.equations import decide_pii
from redibis.pii.thresholds import Thresholds
from redibis.training.registry_hook import (
    JsonTableLearnedBackend,
    LocalLlmInferBackend,
    NullLearnedBackend,
    apply_learned_to_detections,
    learned_backend_from_config,
)


def test_suggest_only_never_auto_applies():
    policy = get_builtin_pack("telecom")
    engine = PolicyEngine(policy)
    # Use a tag that exists in the telecom pack if possible; otherwise any tag
    # still must not land in resolved_tags when suggest_only=True.
    cand = CandidateTag(
        domain="DataSensitivity",
        tag="PII",
        confidence=0.99,
        source="llm_judgment",
        suggest_only=True,
    )
    result = engine.resolve(
        [cand],
        ClassificationContext(table="telecom.customers", column="msisdn"),
    )
    assert "DataSensitivity:PII" not in result.tag_keys()
    assert result.suggestions
    assert result.suggestions[0].suggest_only is True
    assert "DataSensitivity:PII" in result.candidates_dropped


def test_hard_candidate_still_applies():
    policy = get_builtin_pack("telecom")
    engine = PolicyEngine(policy)
    # Contract-sourced hard candidates from the ensemble path.
    ens = ClassifierEnsemble(policy)
    prop = {
        "name": "msisdn",
        "classification": "pii_personal",
        "tags": ["pii"],
        "entity_type": "PHONE_NUMBER",
        "privacy": {"classification": "pii_personal"},
    }
    cands = ens.collect(prop)
    hard = [c for c in cands if not c.suggest_only]
    if not hard:
        pytest.skip("telecom pack produced no hard candidates for fixture prop")
    result = engine.resolve(
        hard,
        ClassificationContext(table="telecom.customers", column="msisdn"),
    )
    assert result.tag_keys()


def test_learned_disabled_never_votes():
    d = PIIDetection(column="msisdn", detected=False, learned_score=0.99)
    t = Thresholds(learned_enabled=False, learned_min=0.90)
    for mode in ("lenient", "independent", "balanced", "strict"):
        assert decide_pii(d, mode, t).detected is False


def test_learned_alone_lenient_blocked_until_promoted():
    d = PIIDetection(column="msisdn", detected=False, learned_score=0.99)
    t = Thresholds(learned_enabled=True, learned_promoted=False, learned_min=0.90)
    assert decide_pii(d, "lenient", t).detected is False
    assert decide_pii(d, "independent", t).detected is False


def test_learned_with_peer_in_balanced():
    d = PIIDetection(
        column="msisdn",
        detected=False,
        learned_score=0.95,
        presidio_score=0.85,
    )
    t = Thresholds(
        learned_enabled=True,
        learned_promoted=False,
        learned_min=0.90,
        presidio_min=0.80,
    )
    assert decide_pii(d, "balanced", t).detected is True


def test_learned_below_floor_no_vote():
    d = PIIDetection(
        column="msisdn",
        detected=False,
        learned_score=0.5,
        presidio_score=0.85,
    )
    t = Thresholds(
        learned_enabled=True,
        learned_promoted=False,
        learned_min=0.90,
        presidio_min=0.80,
    )
    # Only one real vote (presidio) and score below very_high floor → not detected.
    assert decide_pii(d, "balanced", t).detected is False


def test_learned_promoted_can_flip_lenient():
    d = PIIDetection(column="msisdn", detected=False, learned_score=0.95)
    t = Thresholds(learned_enabled=True, learned_promoted=True, learned_min=0.90)
    assert decide_pii(d, "lenient", t).detected is True


def test_apply_learned_does_not_set_detected(tmp_path):
    table = {
        "msisdn": {"score": 0.96, "label": "PHONE_NUMBER", "entity": "PHONE_NUMBER"},
    }
    path = tmp_path / "model.json"
    path.write_text(json.dumps(table), encoding="utf-8")
    backend = JsonTableLearnedBackend(path)
    dets = [PIIDetection(column="msisdn", detected=False, presidio_score=0.5)]
    out = apply_learned_to_detections(dets, backend=backend, table="")
    assert out[0].detected is False
    assert out[0].learned_score == pytest.approx(0.96)
    assert out[0].learned_engine == "learned:json"


def test_null_backend_noop():
    dets = [PIIDetection(column="x", detected=False)]
    out = apply_learned_to_detections(dets, backend=NullLearnedBackend())
    assert out[0].learned_score is None


def test_local_llm_refuses_non_local_residency():
    with pytest.raises(ValueError, match="non-local"):
        LocalLlmInferBackend(residency="public")


def test_learned_backend_from_config_disabled():
    from redibis.config import LearnedClassifierConfig, PIIConfig

    cfg = PIIConfig(learned=LearnedClassifierConfig(enabled=False, model_path="/x"))
    assert isinstance(learned_backend_from_config(cfg), NullLearnedBackend)


def test_ensemble_learned_suggestions_are_suggest_only():
    policy = get_builtin_pack("telecom")
    ens = ClassifierEnsemble(policy)
    cands = ens.collect(
        {"name": "city", "logicalType": "string"},
        learned_suggestions=[{
            "domain": "DataSensitivity",
            "tag": "PII",
            "confidence": 0.91,
        }],
    )
    learned = [c for c in cands if c.source == "learned_classifier"]
    assert learned
    assert all(c.suggest_only for c in learned)
    result = PolicyEngine(policy).resolve(
        learned,
        ClassificationContext(table="t", column="city"),
    )
    assert "DataSensitivity:PII" not in result.tag_keys()
