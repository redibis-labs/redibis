"""Golden coverage for operator call-center transcripts (exact boundaries)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from redibis.pii.eval.span_metrics import DATASET_KIND, validate_dataset
from redibis.pii.rules.ruleset import RuleSetCompiler
from redibis.pii.scan.result import TextScanConfig
from redibis.pii.scan.text_scanner import TextScanner

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "text_gateway" / "operator_call_center.json"

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


def _scanner() -> TextScanner:
    return TextScanner(ruleset=RuleSetCompiler.default())


def test_operator_fixture_is_portable_eval_dataset():
    raw = _dataset()
    assert raw["kind"] == DATASET_KIND
    normalized = validate_dataset(raw, default_language="ar")
    assert len(normalized["cases"]) == 15
    for row in raw["cases"]:
        text = row["text"]
        for span in row["expected_spans"]:
            assert text[span["start"]:span["end"]]


def _find_span(result, start: int, end: int, entity: str):
    for d in result.detections:
        if d.entity_type == entity and d.start == start and d.end == end:
            return d
    return None


def _assert_expected(row, *, cfg):
    text = row["text"]
    scan_cfg = cfg
    if row.get("language") == "en":
        scan_cfg = TextScanConfig(
            engines=cfg.engines,
            language="en",
            min_score=cfg.min_score,
            preprocess_obfuscation=True,
            resolve=cfg.resolve,
        )
    result = _scanner().scan(text, scan_cfg)
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
    return result


@pytest.mark.parametrize("row", _dataset()["cases"], ids=lambda r: r["id"])
@pytest.mark.parametrize("cfg,label", [(BENCH, "bench"), (GATEWAY, "gateway")])
def test_operator_call_center_goldens(row, cfg, label):
    _assert_expected(row, cfg=cfg)
