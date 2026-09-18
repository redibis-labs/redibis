"""trim_span: never grow, never empty, idempotent, leaves golden spans alone."""

from __future__ import annotations

import json
from pathlib import Path

from redibis.pii.trim import trim_span


ROOT = Path(__file__).resolve().parent
SAMPLE = ROOT / "data" / "text_pii" / "datasets" / "sample.json"


def test_trim_span_never_grows_or_empties():
    text = "  Alice,  "
    start, end, _rules = trim_span(text, 0, len(text), entity_type="PERSON")
    assert start >= 0 and end <= len(text)
    assert end > start
    assert (end - start) <= len(text)
    # Already-tight span is unchanged.
    inner = text.index("Alice")
    s2, e2, applied = trim_span(text, inner, inner + 5, entity_type="PERSON")
    assert (s2, e2) == (inner, inner + 5)
    assert applied == []


def test_trim_span_is_idempotent():
    text = "اسمي محمد."
    a = trim_span(text, 0, len(text), entity_type="PERSON")
    b = trim_span(text, a[0], a[1], entity_type="PERSON")
    assert (b[0], b[1]) == (a[0], a[1])


def test_trim_leaves_corpus_golden_spans_unchanged():
    raw = json.loads(SAMPLE.read_text(encoding="utf-8"))
    changed = []
    for case in raw.get("cases") or []:
        text = case["text"]
        for span in case.get("expected_spans") or []:
            start, end = int(span["start"]), int(span["end"])
            et = str(span.get("entity_type") or "")
            ns, ne, _ = trim_span(text, start, end, entity_type=et)
            if (ns, ne) != (start, end):
                changed.append((case["id"], et, start, end, ns, ne, text[start:end]))
    assert changed == []
