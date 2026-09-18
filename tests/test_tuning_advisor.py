"""LLM tuning recommendations — advisory only."""

from __future__ import annotations

import json

from redibis.pii.scan.result import Detection, DetectionResult
from redibis.pii.text_batch import BatchDocument, BatchRunConfig, run_text_batch
from redibis.pii.text_rules import default_text_rules
from redibis.pii.tuning_advisor import (
    Recommendation,
    RecommendationSet,
    aggregate,
    recommend,
    validate,
)


def test_recommendations_are_never_applied_automatically():
    overlay = default_text_rules()
    before = overlay.to_dict()

    class Refiner:
        def _call_model(self, prompt, system="", model_role=""):
            return json.dumps({
                "recommendations": [
                    {"target": "noise_terms", "value": "افندم", "action": "add", "reason": "filler"},
                ]
            })

    recs = recommend("hello", overlay=overlay, refiner=Refiner())
    assert recs.applied is False
    assert overlay.to_dict() == before
    assert recs.items


def test_invalid_regex_is_kept_and_marked():
    recs = RecommendationSet(items=(
        Recommendation(target="patterns", action="propose", value={"pattern": "["}),
    ))
    out = validate(recs, text="hello world")
    assert len(out.items) == 1
    assert out.items[0].validation.get("ok") is False
    assert out.items[0].validation.get("invalid_regex")


def test_overbroad_pattern_is_flagged():
    recs = RecommendationSet(items=(
        Recommendation(target="patterns", action="propose", value={"pattern": "."}),
    ))
    out = validate(recs, text="abcdefghij klmnop")
    assert out.items[0].validation.get("overbroad") is True


def test_term_that_would_remove_an_accepted_span_is_flagged():
    text = "Zorpzorp called"
    spans = [{"start": 0, "end": 8, "entity_type": "PERSON", "text": "Zorpzorp", "engine": "ner"}]
    recs = RecommendationSet(items=(
        Recommendation(target="exclude_terms", action="add", value="Zorpzorp"),
    ))
    out = validate(recs, text=text, spans=spans, accepted=spans)
    flags = out.items[0].validation
    assert flags.get("would_remove_accepted") is True


def test_reasons_are_scrubbed():
    recs = RecommendationSet(items=(
        Recommendation(
            target="noise_terms",
            action="add",
            value="ok",
            reason="caller 01234567890 said ok",
        ),
    ))
    out = validate(recs, text="ok")
    assert "01234567890" not in out.items[0].reason


def test_aggregate_sums_support_and_dedups_by_target_value():
    a = RecommendationSet(items=(
        Recommendation(target="noise_terms", action="add", value="افندم"),
    ))
    b = RecommendationSet(items=(
        Recommendation(target="noise_terms", action="add", value="افندم"),
        Recommendation(target="exclude_terms", action="add", value="agent"),
    ))
    combined = aggregate([a, b])
    by = {(r.target, r.value): r.support for r in combined.items}
    assert by[("noise_terms", "افندم")] == 2
    assert by[("exclude_terms", "agent")] == 1
    assert combined.items[0].support >= combined.items[-1].support
    assert combined.applied is False


def test_batch_writes_engine_and_llm_json_side_by_side(tmp_path):
    class Svc:
        def scan(self, text, **kwargs):
            return DetectionResult(
                kind="span",
                detections=(Detection("EMAIL_ADDRESS", 0.9, "regex", 0, 5, text[:5]),),
                entity_counts={"EMAIL_ADDRESS": 1},
                ruleset_id="builtin",
                ruleset_version="1",
                language="en",
                llm_verdict={
                    "kind": "redibis.llm_verdict",
                    "mode": "independent",
                    "spans": [{"start": 0, "end": 5, "entity_type": "EMAIL_ADDRESS", "text": text[:5]}],
                },
                recommendations={
                    "kind": "redibis.tuning_recommendations",
                    "applied": False,
                    "items": [{"target": "noise_terms", "value": "ok", "action": "add"}],
                },
            )

    docs = [
        BatchDocument(doc_id="a", sanitized_id="a", source="a.txt", text="a@x.co"),
        BatchDocument(doc_id="b", sanitized_id="b", source="b.txt", text="b@x.co"),
    ]
    out = tmp_path / "out"
    run_text_batch(
        Svc(),
        docs,
        BatchRunConfig(llm_verdict="independent", recommend=True, engines="regex"),
        out_dir=out,
    )
    assert (out / "documents" / "a.json").is_file()
    assert (out / "documents" / "a.llm.json").is_file()
    assert (out / "documents" / "b.llm.json").is_file()
    assert (out / "llm-verdicts.jsonl").is_file()
    assert (out / "verdict-diff.csv").is_file()
    assert (out / "recommendations" / "a.json").is_file()
    assert (out / "recommendations.aggregate.json").is_file()
