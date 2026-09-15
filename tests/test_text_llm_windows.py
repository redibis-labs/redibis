"""D1: LLM prompts use window-relative offsets; rebasing happens once."""

from __future__ import annotations

import json
import re

from redibis.pii.ner_window import windows
from redibis.pii.scan.result import Candidate, TextScanConfig
from redibis.pii.text_llm import LlmTextRefiner, _candidate_summary

NEEDLE = "user@hostxx"  # 11 chars, no '.' so window snapping cannot clip it
ABS_START = 5605
DOC_LEN = 6822
TARGET = 3500
OVERLAP = 300


def _later_window_doc() -> tuple[str, int, int, int]:
    """A 6822-char document whose needle sits in window 1, not window 0."""
    suffix = DOC_LEN - ABS_START - len(NEEDLE)
    text = ("n" * ABS_START) + NEEDLE + ("n" * suffix)
    assert len(text) == DOC_LEN
    wins = windows(text, target_chars=TARGET, overlap_chars=OVERLAP)
    later = [
        i for i, w in enumerate(wins)
        if i > 0 and w.start < ABS_START + len(NEEDLE) and ABS_START < w.end
    ]
    assert later, "needle must land in a later window"
    return text, later[0], wins[later[0]].start, len(wins[later[0]].text)


def _cand(text: str) -> Candidate:
    end = ABS_START + len(NEEDLE)
    return Candidate(
        entity_type="EMAIL_ADDRESS",
        score=0.9,
        engine="regex",
        start=ABS_START,
        end=end,
        text=text[ABS_START:end],
        recognizer="regex",
    )


def _cfg() -> TextScanConfig:
    return TextScanConfig(
        engines="regex",
        language="en",
        use_llm=True,
        llm_window_chars=TARGET,
        llm_window_overlap=OVERLAP,
        llm_max_windows=8,
        min_score=0.2,
    )


def test_candidate_summary_prints_window_relative_offsets():
    text, _i, win_start, win_len = _later_window_doc()
    blob = _candidate_summary([_cand(text)], text, offset=win_start, window_len=win_len)
    rel_start = ABS_START - win_start
    rel_end = rel_start + len(NEEDLE)
    assert f"[{rel_start}:{rel_end}]" in blob
    assert f"[{ABS_START}:{ABS_START + len(NEEDLE)}]" not in blob


def test_candidate_summary_slices_surface_from_absolute_offsets():
    text, _i, win_start, win_len = _later_window_doc()
    blob = _candidate_summary([_cand(text)], text, offset=win_start, window_len=win_len)
    assert NEEDLE in blob
    rel_start = ABS_START - win_start
    rel_end = rel_start + len(NEEDLE)
    wrong_surface = text[rel_start:rel_end]
    assert wrong_surface != NEEDLE
    assert wrong_surface not in blob


def test_candidate_straddling_a_window_edge_is_clamped_or_dropped():
    text, _i, win_start, win_len = _later_window_doc()
    # Entirely to the left of this window → dropped (relative end clamps to 0).
    left = Candidate(
        entity_type="PERSON", score=0.5, engine="regex",
        start=0, end=10, text=text[0:10], recognizer="regex",
    )
    blob = _candidate_summary([left], text, offset=win_start, window_len=win_len)
    assert "[0:10]" not in blob
    assert "PERSON" not in blob

    # Straddles the left edge of window 1: clamp relative start to 0.
    straddle_start = win_start - 20
    straddle_end = win_start + 30
    straddling = Candidate(
        entity_type="LOCATION", score=0.5, engine="regex",
        start=straddle_start, end=straddle_end,
        text=text[straddle_start:straddle_end], recognizer="regex",
    )
    blob = _candidate_summary([straddling], text, offset=win_start, window_len=win_len)
    assert "[0:30]" in blob
    assert f"[{straddle_start}:{straddle_end}]" not in blob
    assert text[straddle_start:straddle_end][:48] in blob


def test_llm_span_in_a_later_window_rebases_to_the_right_place():
    text, win_i, win_start, _win_len = _later_window_doc()
    rel_start = ABS_START - win_start
    rel_end = rel_start + len(NEEDLE)
    seen_windows = []

    class Stub:
        def complete(self, system, prompt):
            seen_windows.append(prompt)
            # Window 0 has no needle; only reply for the later window.
            if NEEDLE in prompt.split("Text:\n", 1)[-1][:4000] or f"[{rel_start}:{rel_end}]" in prompt:
                return json.dumps({
                    "spans": [{
                        "start": rel_start,
                        "end": rel_end,
                        "entity_type": "EMAIL_ADDRESS",
                        "score": 0.88,
                    }],
                    "review": [],
                })
            return '{"spans":[],"review":[]}'

    refiner = LlmTextRefiner(provider=Stub())
    out = refiner.propose_spans(text, [_cand(text)], _cfg())
    assert out, "later-window LLM span was dropped"
    hit = out[0]
    assert text[hit.start:hit.end] == NEEDLE
    assert hit.start == ABS_START
    assert hit.end == ABS_START + len(NEEDLE)
    assert win_i > 0
    assert len(seen_windows) > 1


def test_offsets_are_never_rebased_twice():
    """A model that mirrors the candidate-list convention must land on the span.

    Pre-D1 the prompt printed absolute offsets, the stub (and real models) echoed
    them, and ``start + win.start`` threw the span off the document.
    """
    text, _i, win_start, _win_len = _later_window_doc()

    class Echo:
        def complete(self, system, prompt):
            spans = []
            for m in re.finditer(r"- \[(\d+):(\d+)\] (\S+)", prompt):
                spans.append({
                    "start": int(m.group(1)),
                    "end": int(m.group(2)),
                    "entity_type": m.group(3),
                    "score": 0.9,
                })
            return json.dumps({"spans": spans, "review": []})

    refiner = LlmTextRefiner(provider=Echo())
    out = refiner.propose_spans(text, [_cand(text)], _cfg())
    assert out, "echoed offsets were rebased twice and dropped"
    hit = next(c for c in out if c.entity_type == "EMAIL_ADDRESS")
    assert text[hit.start:hit.end] == NEEDLE
    assert hit.start == ABS_START
    blob = _candidate_summary(
        [_cand(text)], text, offset=win_start, window_len=len(text) - win_start,
    )
    assert f"[{ABS_START}:" not in blob
    assert win_start > 0


def test_review_rows_rebase_like_spans():
    text, _i, win_start, _win_len = _later_window_doc()
    existing = [_cand(text)]
    rel_start = ABS_START - win_start
    rel_end = rel_start + len(NEEDLE)
    refiner = LlmTextRefiner()
    got = refiner._coerce_review(
        {
            "start": rel_start,
            "end": rel_end,
            "verdict": "PII",
            "entity_type": "EMAIL_ADDRESS",
            "confidence": 0.9,
            "reason": "email",
        },
        text=text,
        offset=win_start,
        existing=existing,
    )
    assert got is not None
    assert got.start == ABS_START
    assert got.end == ABS_START + len(NEEDLE)
    # A review whose rebased span matches no candidate is rejected.
    assert refiner._coerce_review(
        {"start": 0, "end": 4, "verdict": "NOT_PII", "confidence": 1.0, "reason": "x"},
        text=text,
        offset=win_start,
        existing=existing,
    ) is None


def test_prompt_does_not_mix_absolute_and_relative_offsets():
    text, _i, win_start, _win_len = _later_window_doc()
    prompts: list[str] = []

    class Capture:
        def complete(self, system, prompt):
            prompts.append(prompt)
            return '{"spans":[],"review":[]}'

    LlmTextRefiner(provider=Capture()).propose_spans(text, [_cand(text)], _cfg())
    later = [p for p in prompts if NEEDLE in p or f"EMAIL_ADDRESS" in p]
    assert later
    for prompt in later:
        assert f"[{ABS_START}:{ABS_START + len(NEEDLE)}]" not in prompt
        assert "first character of the Text block is offset 0" in prompt
        assert "Existing candidates below" in prompt
        assert "use the same convention" in prompt
