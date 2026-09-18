"""Independent LLM verdict — text + entity vocabulary, no engine priming."""

from __future__ import annotations

import json

from redibis.pii.llm_verdict import (
    LlmVerdict,
    LlmVerdictSpan,
    diff_against_engine,
    independent_prompt,
    independent_system,
    run_llm_verdict,
)
from redibis.pii.ner_window import windows
from redibis.pii.scan.result import Detection, TextScanConfig
from redibis.pii.scan.text_scanner import TextScanner
from redibis.pii.rules.ruleset import RuleSetCompiler


def test_independent_prompt_contains_no_engine_candidates():
    prompt = independent_prompt("hello Alice")
    system = independent_system("en")
    blob = prompt + "\n" + system
    assert "Existing engines found" not in blob
    assert "_candidate_summary" not in blob
    assert "You are not shown any engine candidates" in prompt
    assert "You are not shown any engine hits" in system


def test_verdict_spans_satisfy_slice_integrity():
    text = "xxAliceyy"

    class Refiner:
        def _call_model(self, prompt, system=""):
            start = prompt.split("Text:\n", 1)[1].split("\n\n", 1)[0].index("Alice")
            return json.dumps({
                "spans": [{"start": start, "end": start + 5, "entity_type": "PERSON",
                           "confidence": 0.9, "reason": "name"}],
                "not_pii": [],
            })

    verdict = run_llm_verdict(text, refiner=Refiner())
    assert verdict.error == ""
    assert verdict.spans
    for span in verdict.spans:
        assert text[span.start:span.end] == span.text


def test_verdict_in_later_window_rebases_correctly():
    text = ("n" * 40) + "Alice" + ("n" * 40)
    cfg = TextScanConfig(llm_window_chars=32, llm_window_overlap=4, llm_max_windows=8)
    wins = windows(text, target_chars=32, overlap_chars=4)
    assert len(wins) > 1
    abs_start = text.index("Alice")

    class Refiner:
        def _call_model(self, prompt, system=""):
            body = prompt.split("Text:\n", 1)[1].split("\n\n", 1)[0]
            idx = body.find("Alice")
            if idx < 0:
                return '{"spans":[],"not_pii":[]}'
            return json.dumps({
                "spans": [{"start": idx, "end": idx + 5, "entity_type": "PERSON",
                           "confidence": 0.8, "reason": "name"}],
                "not_pii": [],
            })

    verdict = run_llm_verdict(text, config=cfg, refiner=Refiner())
    assert any(s.start == abs_start and s.end == abs_start + 5 for s in verdict.spans)


def test_verdict_error_is_recorded_not_swallowed():
    class Boom:
        def _call_model(self, prompt, system=""):
            raise RuntimeError("model down")

    verdict = run_llm_verdict("hello", refiner=Boom())
    assert verdict.error
    assert "model down" in verdict.error
    assert verdict.spans == ()


def test_engines_none_runs_no_deterministic_engine():
    scanner = TextScanner(ruleset=RuleSetCompiler.default())
    result = scanner.scan(
        "alice@example.com",
        TextScanConfig(engines="none", min_score=0.2),
    )
    assert "regex" not in result.engines_ran
    assert "phone" not in result.engines_ran
    assert "ner" not in result.engines_ran
    assert result.detections == ()


def test_verdict_reason_is_scrubbed():
    class Refiner:
        def _call_model(self, prompt, system=""):
            return json.dumps({
                "spans": [{"start": 0, "end": 5, "entity_type": "PERSON",
                           "confidence": 0.9, "reason": "digits 01234567890"}],
                "not_pii": [],
            })

    verdict = run_llm_verdict("Alice", refiner=Refiner())
    assert verdict.spans
    assert "01234567890" not in verdict.spans[0].reason


def test_cloud_provider_still_refused_for_verdict():
    from redibis.pii.text_llm import LlmTextRefiner

    class Cloud:
        def _call_model(self, prompt, system=""):
            LlmTextRefiner(allow_cloud=False)._assert_local_provider("openai")
            return "{}"

    verdict = run_llm_verdict("hello", refiner=Cloud())
    assert verdict.error
    assert "cloud" in verdict.error.lower() or "not allowed" in verdict.error.lower()


def test_diff_against_engine_classes():
    verdict = LlmVerdict(spans=(
        LlmVerdictSpan(0, 5, "PERSON", 0.9, "Alice"),
        LlmVerdictSpan(10, 13, "PERSON", 0.9, "Bob"),
    ))
    engine = [
        Detection("PERSON", 0.9, "ner", 0, 5, "Alice"),
        Detection("LOCATION", 0.9, "ner", 20, 24, "Cairo"),
    ]
    diff = diff_against_engine(verdict, engine)
    assert diff["agree"] == 1
    assert diff["llm_only"] == 1
    assert diff["engine_only"] == 1
