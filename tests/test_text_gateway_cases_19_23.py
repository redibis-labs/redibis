"""Golden coverage for Text Gateway cases 19–23 (exact boundaries)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from redibis.pii.eval.span_metrics import DATASET_KIND, validate_dataset
from redibis.pii.rules.ruleset import RuleSetCompiler
from redibis.pii.scan.result import TextScanConfig
from redibis.pii.scan.text_scanner import TextScanner

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "text_gateway" / "cases_19_23.json"

BENCH = TextScanConfig(
    engines="regex",
    language="ar",
    min_score=0.2,
    preprocess_obfuscation=True,
    resolve="priority",
)
GATEWAY = TextScanConfig(
    engines="both",
    language="ar",
    min_score=0.35,
    preprocess_obfuscation=True,
    resolve="priority",
)


def _dataset() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _case(case_id: str) -> dict:
    for row in _dataset()["cases"]:
        if row["id"] == case_id:
            return row
    raise KeyError(case_id)


def _scanner() -> TextScanner:
    return TextScanner(ruleset=RuleSetCompiler.default())


def test_fixture_is_portable_eval_dataset():
    raw = _dataset()
    assert raw["kind"] == DATASET_KIND
    normalized = validate_dataset(raw, default_language="ar")
    assert len(normalized["cases"]) == 5


def _assert_slice_integrity(text: str, result) -> None:
    for d in result.detections:
        if d.start is None or d.end is None:
            continue
        assert text[d.start:d.end] == d.text, (
            d.entity_type, d.start, d.end, d.text, text[d.start:d.end]
        )


def _inject_fillers(text: str, start: int, end: int, a: str, b: str) -> tuple[str, int, int]:
    inner = text[start:end]
    parts = inner.split()
    if len(parts) < 4:
        raise AssertionError(f"span too short to inject fillers: {inner!r}")
    i1 = max(1, len(parts) // 3)
    i2 = max(i1 + 1, (2 * len(parts)) // 3)
    new_inner = " ".join(parts[:i1] + [a] + parts[i1:i2] + [b] + parts[i2:])
    new_text = text[:start] + new_inner + text[end:]
    return new_text, start, start + len(new_inner)


def _find_span(result, start: int, end: int, entity: str):
    for d in result.detections:
        if d.entity_type == entity and d.start == start and d.end == end:
            return d
    return None


def _assert_expected(case_id, *, cfg, extra_check=None, case=None):
    row = case or _case(case_id)
    text = row["text"]
    result = _scanner().scan(text, cfg)
    _assert_slice_integrity(text, result)
    missing, drifted = [], []
    for span in row["expected_spans"]:
        if _find_span(result, span["start"], span["end"], span["entity_type"]):
            continue
        near = [
            d
            for d in result.detections
            if d.entity_type == span["entity_type"]
            and d.start is not None
            and d.end is not None
            and max(0, min(d.end, span["end"]) - max(d.start, span["start"])) > 0
        ]
        (drifted if near else missing).append(
            (
                span["entity_type"],
                text[span["start"]:span["end"]],
                [(d.start, d.end, text[d.start:d.end]) for d in near],
            )
        )
    assert not missing, ("NOT DETECTED", missing)
    assert not drifted, ("BOUNDARY DRIFT", drifted)
    if extra_check:
        extra_check(text, result)
    return result


@pytest.mark.parametrize("cfg,label", [(BENCH, "bench"), (GATEWAY, "gateway")])
def test_case_19_phone_and_arabic_name(cfg, label):
    def _no_agent(text, result):
        agents = [
            d
            for d in result.detections
            if (d.text or "").strip().rstrip(":").casefold() == "agent"
        ]
        assert not agents
        if cfg.engines == "both":
            assert "ner" in (result.engines_unavailable or {}) or "ner" in result.engines_ran

    _assert_expected("case-19", cfg=cfg, extra_check=_no_agent)


@pytest.mark.parametrize("cfg,label", [(BENCH, "bench"), (GATEWAY, "gateway")])
def test_case_20_full_address_and_spoken_phone(cfg, label):
    _assert_expected("case-20", cfg=cfg)


@pytest.mark.parametrize("cfg,label", [(BENCH, "bench"), (GATEWAY, "gateway")])
def test_case_21_voucher_not_nid_and_phone_not_price(cfg, label):
    def _no_price(text, result):
        prices = [
            d
            for d in result.detections
            if d.text
            and "100" in d.text
            and "جنيه" in text[max(0, (d.start or 0) - 2):(d.end or 0) + 8]
        ]
        assert not prices

    _assert_expected("case-21", cfg=cfg, extra_check=_no_price)


@pytest.mark.parametrize("cfg,label", [(BENCH, "bench"), (GATEWAY, "gateway")])
def test_case_22_spoken_nid_and_puk(cfg, label):
    _assert_expected("case-22", cfg=cfg)


@pytest.mark.parametrize("cfg,label", [(BENCH, "bench"), (GATEWAY, "gateway")])
def test_case_23_ticket_name_and_spoken_phone(cfg, label):
    _assert_expected("case-23", cfg=cfg)


def test_widened_gold_fails_as_boundary_drift():
    row = json.loads(json.dumps(_case("case-20")))
    row["expected_spans"][0]["end"] = int(row["expected_spans"][0]["end"]) + 5
    with pytest.raises(AssertionError) as exc:
        _assert_expected("case-20", cfg=BENCH, case=row)
    assert "BOUNDARY DRIFT" in str(exc.value)


@pytest.mark.parametrize("case_id", ["case-20", "case-23"])
@pytest.mark.parametrize("cfg,label", [(BENCH, "bench"), (GATEWAY, "gateway")])
def test_filler_injection_keeps_spoken_phone_canonical(case_id, cfg, label):
    """ايوة / تمام inside a dictated run must not split the phone.

    Middle fillers stay inside a contiguous ``text[start:end]`` span (Unicode
    offsets cannot punch a hole). Leading/trailing fillers are stripped.
    Canonical digits must match the uninjected scan.
    """
    row = _case(case_id)
    text = row["text"]
    gold = next(s for s in row["expected_spans"] if s["entity_type"] == "PHONE_NUMBER")
    baseline = _scanner().scan(text, cfg)
    _assert_slice_integrity(text, baseline)
    base_phone = next(
        d for d in baseline.detections
        if d.entity_type == "PHONE_NUMBER"
        and max(0, min(d.end, gold["end"]) - max(d.start, gold["start"])) > 0
    )
    injected, new_start, new_end = _inject_fillers(
        text, gold["start"], gold["end"], "ايوة", "تمام"
    )
    result = _scanner().scan(injected, cfg)
    _assert_slice_integrity(injected, result)
    phones = [
        d for d in result.detections
        if d.entity_type == "PHONE_NUMBER"
        and max(0, min(d.end, new_end) - max(d.start, new_start)) > 0
    ]
    assert phones, "filler split the spoken-digit run"
    got = phones[0]
    assert (got.canonical or base_phone.canonical) == (base_phone.canonical or got.canonical)
    assert (got.end - got.start) <= (new_end - new_start)
