"""Golden coverage for Text Gateway cases 19–23 (regex + preprocess)."""

from __future__ import annotations

import json
from pathlib import Path

from redibis.pii.eval.span_metrics import DATASET_KIND, validate_dataset
from redibis.pii.rules.ruleset import RuleSetCompiler
from redibis.pii.scan.result import TextScanConfig
from redibis.pii.scan.text_scanner import TextScanner

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "text_gateway" / "cases_19_23.json"


def _dataset() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _case(case_id: str) -> dict:
    for row in _dataset()["cases"]:
        if row["id"] == case_id:
            return row
    raise KeyError(case_id)


def _scanner() -> TextScanner:
    return TextScanner(ruleset=RuleSetCompiler.default())


def _cfg() -> TextScanConfig:
    return TextScanConfig(
        engines="regex",
        language="ar",
        min_score=0.2,
        preprocess_obfuscation=True,
        resolve="priority",
    )


def test_fixture_is_portable_eval_dataset():
    raw = _dataset()
    assert raw["kind"] == DATASET_KIND
    normalized = validate_dataset(raw, default_language="ar")
    assert len(normalized["cases"]) == 5


def _has_span(result, text: str, needle: str, entity: str) -> bool:
    for d in result.detections:
        if d.entity_type != entity or d.start is None or d.end is None:
            continue
        if text[d.start:d.end] == needle:
            return True
        if needle in (d.text or ""):
            return True
    return False


def _assert_expected(case_id: str, *, extra_check=None):
    row = _case(case_id)
    text = row["text"]
    result = _scanner().scan(text, _cfg())
    missing = []
    for span in row["expected_spans"]:
        needle = text[span["start"]:span["end"]]
        if not _has_span(result, text, needle, span["entity_type"]):
            missing.append((span["entity_type"], needle))
    assert not missing, (
        missing,
        [(d.entity_type, d.text, d.recognizer) for d in result.detections],
    )
    if extra_check:
        extra_check(text, result)


def test_case_19_phone_and_arabic_name():
    def _no_agent(text, result):
        agents = [
            d for d in result.detections
            if (d.text or "").strip().rstrip(":").casefold() == "agent"
        ]
        assert not agents

    _assert_expected("case-19", extra_check=_no_agent)


def test_case_20_full_address_and_spoken_phone():
    _assert_expected("case-20")


def test_case_21_voucher_not_nid_and_phone_not_price():
    def _no_price(text, result):
        prices = [
            d for d in result.detections
            if d.text and "100" in d.text
            and "جنيه" in text[max(0, (d.start or 0) - 2):(d.end or 0) + 8]
        ]
        assert not prices

    _assert_expected("case-21", extra_check=_no_price)


def test_case_22_spoken_nid_and_puk():
    _assert_expected("case-22")


def test_case_23_ticket_name_and_spoken_phone():
    _assert_expected("case-23")
