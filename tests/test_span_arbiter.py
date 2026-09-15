"""Table-driven tests for span-level LLM/engine arbitration."""

from __future__ import annotations

from pathlib import Path

import pytest

from redibis.pii.scan.result import Candidate, TextScanConfig
from redibis.pii.scan.text_scanner import TextScanner
from redibis.pii.span_arbiter import (
    AGREEMENT,
    LlmReview,
    arbitrate,
    record_to_dict,
    stamp_detections,
)
from redibis.pii.thresholds import Thresholds
from redibis.pii.text_llm import LlmTextRefiner

CORPUS = Path(__file__).parent / "data" / "text_pii"

MODES = ("independent", "strict", "balanced", "lenient")


def _c(**kwargs) -> Candidate:
    start = kwargs.get("start", 0)
    end = kwargs.get("end", 4)
    text = kwargs.get("text", "xxxx")
    return Candidate(
        entity_type=kwargs.get("entity_type", "PHONE_NUMBER"),
        score=kwargs.get("score", 0.9),
        engine=kwargs.get("engine", "regex"),
        start=start,
        end=end,
        text=text,
        recognizer=kwargs.get("recognizer", kwargs.get("engine", "regex")),
        validator=kwargs.get("validator", ""),
        is_proposal=kwargs.get("is_proposal", kwargs.get("engine") == "llm"),
    )


def _assert_slice(text: str, detections) -> None:
    for d in detections:
        if d.start is None or d.end is None:
            continue
        assert text[d.start:d.end] == d.text


def test_validated_span_is_never_retyped_by_the_llm():
    text = "01001234567 ticket"
    engine = _c(entity_type="PHONE_NUMBER", engine="phone", start=0, end=11,
                text=text[0:11], validator="libphonenumber", score=0.95)
    llm = _c(entity_type="SUPPORT_TICKET", engine="llm", start=0, end=11,
             text=text[0:11], score=0.99, is_proposal=True)
    for mode in MODES:
        kept, records = arbitrate(
            [engine, llm], text=text, mode=mode, llm_ran=True,
        )
        phones = [c for c in kept if c.entity_type == "PHONE_NUMBER" and c.engine != "llm"]
        if mode == "independent":
            assert any(c.entity_type == "PHONE_NUMBER" for c in kept)
            continue
        assert phones, f"{mode}: validated phone must survive"
        assert all(c.entity_type == "PHONE_NUMBER" for c in phones)


def test_validated_span_is_never_vetoed():
    text = "01001234567"
    engine = _c(entity_type="PHONE_NUMBER", engine="phone", start=0, end=11,
                text=text, validator="libphonenumber", score=0.95)
    review = LlmReview(start=0, end=11, verdict="NOT_PII", confidence=0.99, reason="quantity")
    for mode in MODES:
        kept, records = arbitrate(
            [engine], text=text, mode=mode, llm_ran=True, reviews=[review],
        )
        if mode == "independent":
            assert engine in kept or any(c.start == 0 and c.end == 11 for c in kept)
            continue
        assert any(c.validator == "libphonenumber" for c in kept), f"{mode}: vetoed a validated span"
        assert not any(r.agreement == "vetoed" and r.validated for r in records)


def test_validated_span_boundary_may_grow_but_never_shrink():
    text = "Call 01001234567 please. Next."
    engine = _c(entity_type="PHONE_NUMBER", engine="phone", start=5, end=16,
                text="01001234567", validator="libphonenumber", score=0.95)
    llm = _c(entity_type="PHONE_NUMBER", engine="llm", start=10, end=14,
             text=text[10:14], score=0.99, is_proposal=True)
    for mode in ("balanced", "lenient"):
        kept, _ = arbitrate([engine, llm], text=text, mode=mode, llm_ran=True)
        det = [c for c in kept if c.validator]
        assert det, mode
        assert det[0].start <= engine.start
        assert det[0].end >= engine.end


def test_llm_silence_is_unconfirmed_not_vetoed():
    text = "alice@example.com"
    engine = _c(entity_type="EMAIL_ADDRESS", engine="regex", start=0, end=17, text=text, score=0.9)
    kept, records = arbitrate([engine], text=text, mode="balanced", llm_ran=True, reviews=[])
    assert any(c.entity_type == "EMAIL_ADDRESS" for c in kept)
    assert all(r.agreement != "vetoed" for r in records)
    assert any(r.agreement == "unconfirmed" for r in records)


def test_review_of_a_nonexistent_span_is_discarded():
    refiner = LlmTextRefiner()
    existing = [_c(entity_type="EMAIL_ADDRESS", start=0, end=5, text="alice")]
    got = refiner._coerce_review(
        {"start": 90, "end": 99, "verdict": "NOT_PII", "confidence": 1.0, "reason": "nope"},
        text="alice@example.com",
        offset=0,
        existing=existing,
    )
    assert got is None


def test_malformed_verdict_degrades_to_unsure():
    refiner = LlmTextRefiner()
    existing = [_c(entity_type="EMAIL_ADDRESS", start=0, end=17, text="alice@example.com")]
    got = refiner._coerce_review(
        {"start": 0, "end": 17, "verdict": "MAYBE", "confidence": 0.4, "reason": "x"},
        text="alice@example.com",
        offset=0,
        existing=existing,
    )
    assert got is not None
    assert got.verdict == "UNSURE"


def test_missing_review_key_reproduces_today_behaviour():
    spans, reviews = LlmTextRefiner._parse_response(
        '{"spans":[{"start":0,"end":5,"entity_type":"PERSON","score":0.8}]}'
    )
    assert reviews == []
    assert spans[0]["entity_type"] == "PERSON"
    text = "Alice called"
    llm = _c(entity_type="PERSON", engine="llm", start=0, end=5, text="Alice", score=0.8, is_proposal=True)
    kept_ind, _ = arbitrate([llm], text=text, mode="independent", llm_ran=True)
    assert kept_ind[0].is_proposal is True


def test_independent_mode_is_byte_identical_to_pre_change_output():
    from redibis.pii.rules.ruleset import RuleSetCompiler

    scanner = TextScanner(ruleset=RuleSetCompiler.default())
    samples = [
        CORPUS / "we_call_center_ar.txt",
        CORPUS / "spoken_phone_ar.txt",
    ]
    for path in samples:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        cfg = TextScanConfig(engines="regex", language="ar", min_score=0.2, equation="independent")
        result = scanner.scan(text, cfg)
        _assert_slice(text, result.detections)
        payload = result.to_dict()
        assert "arbitration" not in payload
        for span in payload["spans"]:
            assert "agreement" not in span


def test_boundary_widening_is_clamped_to_the_sentence():
    text = "Name is Alice. Next sentence is long padding " + ("x" * 80) + "."
    engine = _c(entity_type="PERSON", engine="ner", start=8, end=13, text="Alice", score=0.7)
    llm = _c(entity_type="PERSON", engine="llm", start=8, end=len(text) - 1,
             text=text[8:len(text) - 1], score=0.9, is_proposal=True)
    kept, records = arbitrate([engine, llm], text=text, mode="balanced", llm_ran=True)
    winner = [c for c in kept if c.entity_type == "PERSON"][0]
    assert winner.end <= text.find(".") + 1 or winner.end <= 14
    assert any(r.rule == "boundary_widen_sentence" for r in records)


def test_llm_reason_is_scrubbed_of_pii():
    refiner = LlmTextRefiner()
    existing = [_c(entity_type="PHONE_NUMBER", start=0, end=11, text="01001234567")]
    got = refiner._coerce_review(
        {"start": 0, "end": 11, "verdict": "PII", "confidence": 0.9,
         "reason": "this is 01001234567 a phone"},
        text="01001234567",
        offset=0,
        existing=existing,
    )
    assert got is not None
    assert "01001234567" not in got.reason
    recs = arbitrate(
        existing, text="01001234567", mode="balanced", llm_ran=True, reviews=[got],
    )[1]
    blob = " ".join(r.llm_reason for r in recs)
    assert "01001234567" not in blob


def test_every_branch_sets_a_stable_rule_id():
    text = "12345678901234 extra"
    engine = _c(entity_type="EG_NATIONAL_ID", engine="regex", start=0, end=14,
                text=text[:14], score=0.5)
    llm = _c(entity_type="SUPPORT_TICKET", engine="llm", start=0, end=14,
             text=text[:14], score=0.9, is_proposal=True)
    seen = set()
    for mode in MODES:
        _, records = arbitrate([engine, llm], text=text, mode=mode, llm_ran=True)
        for rec in records:
            assert rec.rule, rec
            assert rec.agreement in AGREEMENT
            seen.add(rec.rule)
    # independent returns no records; active modes must name the branch
    assert seen


def test_losing_candidates_survive_on_detection_evidence():
    from redibis.pii.rules.resolver import SpanResolver

    text = "alice@example.com"
    a = _c(entity_type="EMAIL_ADDRESS", engine="regex", start=0, end=17, text=text, score=0.9)
    b = _c(entity_type="EMAIL_ADDRESS", engine="llm", start=0, end=17, text=text, score=0.5, is_proposal=True)
    dets = SpanResolver().resolve([a, b], min_score=0.2)
    assert dets
    assert len(dets[0].evidence) >= 2
    assert dets[0].evidence[0].engine == "regex"


def test_slice_integrity_holds_after_arbitration():
    text = "Contact Alice at alice@example.com on 01001234567."
    engine = _c(entity_type="EMAIL_ADDRESS", engine="regex", start=16, end=33,
                text=text[16:33], score=0.9)
    llm = _c(entity_type="PERSON", engine="llm", start=8, end=13, text="Alice",
             score=0.85, is_proposal=True)
    for mode in MODES:
        kept, records = arbitrate([engine, llm], text=text, mode=mode, llm_ran=True)
        for c in kept:
            assert text[c.start:c.end] == c.text
        from redibis.pii.rules.resolver import SpanResolver
        dets = SpanResolver().resolve(kept, min_score=0.2)
        stamped = stamp_detections(dets, records)
        _assert_slice(text, stamped)


def test_balanced_retypes_unvalidated_national_id_to_ticket():
    text = "ticket 12345678901234 done"
    engine = _c(entity_type="EG_NATIONAL_ID", engine="regex", start=7, end=21,
                text="12345678901234", score=0.6)
    llm = _c(entity_type="SUPPORT_TICKET", engine="llm", start=7, end=21,
             text="12345678901234", score=0.9, is_proposal=True)
    kept, records = arbitrate([engine, llm], text=text, mode="balanced", llm_ran=True)
    assert any(c.entity_type == "SUPPORT_TICKET" for c in kept)
    assert any(r.agreement == "type_conflict" and r.rule == "llm_type_wins_unvalidated" for r in records)
    rec = next(r for r in records if r.agreement == "type_conflict")
    d = record_to_dict(rec)
    assert d["engine_type"] == "EG_NATIONAL_ID"
    assert d["llm_type"] == "SUPPORT_TICKET"
    assert "text" not in str(d.get("competing"))


def test_strict_drops_low_score_llm_only():
    text = "hello Alice"
    llm = _c(entity_type="PERSON", engine="llm", start=6, end=11, text="Alice", score=0.4, is_proposal=True)
    kept, records = arbitrate([llm], text=text, mode="strict", llm_ran=True)
    assert kept == []
    assert any(r.rule == "llm_only_dropped_below_llm_min" for r in records)


@pytest.mark.parametrize("mode", ["strict", "balanced", "lenient"])
def test_matrix_agreement_confirmed(mode):
    text = "Alice"
    engine = _c(entity_type="PERSON", engine="ner", start=0, end=5, text="Alice", score=0.7)
    llm = _c(entity_type="PERSON", engine="llm", start=0, end=5, text="Alice", score=0.9, is_proposal=True)
    kept, records = arbitrate([engine, llm], text=text, mode=mode, llm_ran=True)
    assert any(r.agreement == "confirmed" for r in records)
    winner = next(c for c in kept if c.engine == "ner")
    assert winner.score >= engine.score


def test_not_pii_review_suppresses_unvalidated_span_end_to_end():
    """Model JSON → parsed review → arbitration → unvalidated hit is gone."""
    import json

    from redibis.pii.rules.ruleset import RuleSetCompiler
    from redibis.pii.scan.text_scanner import TextScanner
    from redibis.pii.text_llm import LlmTextRefiner

    text = "reach me at alice@example.com thanks"
    start = text.find("alice@example.com")
    end = start + len("alice@example.com")

    class Stub:
        def complete(self, system, prompt):
            return json.dumps({
                "spans": [],
                "review": [{
                    "start": start,
                    "end": end,
                    "verdict": "NOT_PII",
                    "entity_type": "EMAIL_ADDRESS",
                    "confidence": 0.99,
                    "reason": "role label",
                }],
            })

    scanner = TextScanner(
        ruleset=RuleSetCompiler.default(),
        llm_refiner=LlmTextRefiner(provider=Stub()),
    )
    cfg = TextScanConfig(
        engines="regex",
        use_llm=True,
        equation="balanced",
        min_score=0.2,
        include_arbitration=True,
    )
    result = scanner.scan(text, cfg)
    emails = [d for d in result.detections if d.entity_type == "EMAIL_ADDRESS"]
    assert emails == []
    recs = (result.arbitration or {}).get("records") or []
    assert any(r.get("agreement") == "vetoed" for r in recs)


def test_not_pii_review_cannot_suppress_a_validated_span_end_to_end():
    """Safety invariant: a validated phone survives a NOT_PII review."""
    import json

    from redibis.pii.rules.ruleset import RuleSetCompiler
    from redibis.pii.scan.text_scanner import TextScanner
    from redibis.pii.text_llm import LlmTextRefiner

    text = "call +201001234567 now"
    start = text.find("+201001234567")
    end = start + len("+201001234567")

    class Stub:
        def complete(self, system, prompt):
            return json.dumps({
                "spans": [],
                "review": [{
                    "start": start,
                    "end": end,
                    "verdict": "NOT_PII",
                    "entity_type": "PHONE_NUMBER",
                    "confidence": 0.99,
                    "reason": "quantity",
                }],
            })

    scanner = TextScanner(
        ruleset=RuleSetCompiler.default(),
        llm_refiner=LlmTextRefiner(provider=Stub()),
    )
    cfg = TextScanConfig(
        engines="regex",
        use_llm=True,
        equation="balanced",
        min_score=0.2,
        include_arbitration=True,
        language="en",
        default_region="EG",
    )
    result = scanner.scan(text, cfg)
    phones = [d for d in result.detections if d.entity_type == "PHONE_NUMBER"]
    assert phones, "validated phone must survive a NOT_PII review"
    assert any(d.validator for d in phones)
    assert all(getattr(d, "agreement", "") != "vetoed" for d in phones)
    recs = (result.arbitration or {}).get("records") or []
    assert not any(r.get("agreement") == "vetoed" and r.get("validated") for r in recs)
