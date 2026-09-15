"""Overlapping NER/LLM windows rebase onto the original text."""

from __future__ import annotations

from types import SimpleNamespace

from redibis.pii.ner_window import Window, map_windows, windows
from redibis.pii.rules.ruleset import RuleSetCompiler
from redibis.pii.scan.result import TextScanConfig
from redibis.pii.scan.text_scanner import TextScanner


def test_windows_split_long_text_with_overlap():
    text = ("حشو. " * 400) + "اسمي محمد."
    wins = windows(text, target_chars=80, overlap_chars=20)
    assert len(wins) > 1
    assert wins[0].start == 0
    assert wins[-1].end == len(text)
    for w in wins:
        assert text[w.start:w.end] == w.text
        assert isinstance(w, Window)


def test_window_offsets_rebase_to_the_original_text():
    text = "aaaa NAME bbbb"
    needle = "NAME"

    def predict(chunk: str):
        i = chunk.find(needle)
        if i < 0:
            return []
        return [{"start": i, "end": i + len(needle), "label": "PERSON", "score": 0.9}]

    spans, cov = map_windows(text, predict, target_chars=6, overlap_chars=2, max_windows=50)
    hit = next(s for s in spans if s["label"] == "PERSON")
    assert text[hit["start"]:hit["end"]] == needle
    assert cov["fraction"] == 1.0


def test_overlap_seam_entity_is_not_duplicated():
    text = "xxxxSEAMYYY"
    # Entity sits on the seam between two 6-char windows with overlap 4.

    def predict(chunk: str):
        i = chunk.find("SEAM")
        if i < 0:
            return []
        return [{"start": i, "end": i + 4, "label": "PERSON", "score": 0.8, "text": "SEAM"}]

    spans, _cov = map_windows(text, predict, target_chars=6, overlap_chars=4)
    people = [s for s in spans if s["label"] == "PERSON"]
    assert len(people) == 1
    assert text[people[0]["start"]:people[0]["end"]] == "SEAM"


def test_coverage_reports_lt_one_when_a_cap_is_hit():
    text = "abcdefghij" * 50

    def predict(_chunk: str):
        return []

    _spans, cov = map_windows(text, predict, target_chars=20, overlap_chars=0, max_windows=2)
    assert cov["windows_scanned"] == 2
    assert cov["windows_total"] > 2
    assert cov["fraction"] < 1.0
    assert "ner" in cov["reasons"]


def test_ner_finds_an_entity_past_the_model_window():
    pad = "حشو. " * 4000
    name = "محمد عبد الرحمن"
    text = pad + f"اسمي {name}."
    assert text.find(name) > int(0.9 * len(text))

    class Stub:
        def analyze_text(self, chunk, labels=None, phrases=None):
            if len(chunk) > 250:
                raise AssertionError(f"backend saw {len(chunk)} chars")
            i = chunk.find(name)
            if i < 0:
                return []
            return [SimpleNamespace(
                start=i, end=i + len(name), label="PERSON", score=0.95,
                text=name, model="stub",
            )]

    cfg = TextScanConfig(
        engines="ner",
        language="ar",
        min_score=0.1,
        ner_window_chars=200,
        ner_window_overlap=40,
        ner_max_windows=200,
    )
    result = TextScanner(
        ruleset=RuleSetCompiler.default(),
        ner_backend=Stub(),
    ).scan(text, cfg)
    people = [d for d in result.detections if d.entity_type == "PERSON"]
    assert people, "NER missed the name past the model window"
    hit = people[0]
    assert text[hit.start:hit.end] == hit.text
    assert name in hit.text
    per = (result.coverage or {}).get("per_engine") or {}
    assert float(per.get("ner") or 0) == 1.0


def test_evaluation_accepts_a_60k_case_and_flags_truncation():
    from redibis.pii.eval.runner import evaluate_with_service
    from redibis.pii.eval.span_metrics import DATASET_KIND, SCHEMA_VERSION_1_2

    email = "alice@example.com"
    text = ("x" * 60_000) + f" Contact {email} please"
    dataset = {
        "kind": DATASET_KIND,
        "schema_version": SCHEMA_VERSION_1_2,
        "id": "long-eval",
        "cases": [{
            "id": "long-60k",
            "text": text,
            "language": "en",
            "expected_spans": [{
                "id": "s1",
                "start": text.find(email),
                "end": text.find(email) + len(email),
                "entity_type": "EMAIL_ADDRESS",
            }],
        }],
    }

    class Wrap:
        def __init__(self):
            self._scanner = TextScanner(ruleset=RuleSetCompiler.default())

        def scan(self, body, **kwargs):
            cfg = TextScanConfig(
                engines="regex",
                language="en",
                min_score=0.2,
                max_chars=int(kwargs.get("max_chars") or 200_000),
            )
            return self._scanner.scan(body, cfg)

        def entities(self):
            return {"entities": [{"entity_type": "EMAIL_ADDRESS"}]}

    full = evaluate_with_service(
        Wrap(), dataset, options={"engines": "regex", "max_chars": 200_000},
    )
    assert not full.get("truncated")
    assert full["exact"]["micro"]["tp"] >= 1

    clipped = evaluate_with_service(
        Wrap(), dataset, options={"engines": "regex", "max_chars": 1_000},
    )
    assert clipped.get("truncated") is True
    assert "long-60k" in clipped.get("truncated_cases", [])
    assert clipped["gates"]["passed"] is False
    assert any(f["tier"] == "coverage" for f in clipped["gates"]["failures"])


def test_evaluation_300k_case_scores_is_flagged_and_fails_the_gate():
    from redibis.pii.eval.runner import evaluate_with_service
    from redibis.pii.eval.span_metrics import DATASET_KIND, SCHEMA_VERSION_1_2

    email = "alice@example.com"
    text = f"Contact {email} please " + ("x" * 300_000)
    dataset = {
        "kind": DATASET_KIND,
        "schema_version": SCHEMA_VERSION_1_2,
        "id": "oversized-eval",
        "cases": [{
            "id": "long-300k",
            "text": text,
            "language": "en",
            "expected_spans": [{
                "id": "s1",
                "start": text.find(email),
                "end": text.find(email) + len(email),
                "entity_type": "EMAIL_ADDRESS",
            }],
        }],
    }

    class Wrap:
        def __init__(self):
            self._scanner = TextScanner(ruleset=RuleSetCompiler.default())

        def scan(self, body, **kwargs):
            cfg = TextScanConfig(
                engines="regex",
                language="en",
                min_score=0.2,
                max_chars=int(kwargs.get("max_chars") or 200_000),
            )
            return self._scanner.scan(body, cfg)

        def entities(self):
            return {"entities": [{"entity_type": "EMAIL_ADDRESS"}]}

    report = evaluate_with_service(
        Wrap(), dataset, options={"engines": "regex", "max_chars": 200_000},
    )
    case = next(c for c in report["cases"] if c["id"] == "long-300k")
    assert case.get("truncated") is True
    assert "long-300k" in report.get("truncated_cases", [])
    assert report["exact"]["micro"]["tp"] >= 1
    assert report["gates"]["passed"] is False
    assert any(f["tier"] == "coverage" for f in report["gates"]["failures"])
