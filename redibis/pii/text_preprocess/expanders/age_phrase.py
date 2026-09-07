"""Deterministic age-phrase expander (EN + AR)."""

from __future__ import annotations

import re

from redibis.pii.rules.recognizers import RecognizeContext
from redibis.pii.text_preprocess.registry import register_text_expander
from redibis.pii.text_preprocess.surface import SurfaceSpan

_PATTERNS = [
    re.compile(r"(?i)(?<!\w)(?:age|aged)\s*[:=]?\s*(\d{1,3})\s*(?:years?|yrs?)?(?!\w)"),
    re.compile(r"(?i)(?<!\w)(\d{1,3})\s*(?:years?\s*old|yrs?\s*old)(?!\w)"),
    re.compile(r"(?<!\w)(?:عمري|سني|عمره|سنها|عنده|عندها|عمر)\s*[:=]?\s*(\d{1,3})\s*(?:سنة|سنه|سنوات)?"),
    re.compile(r"(?<!\w)(\d{1,3})\s*(?:سنة|سنه|سنوات)(?!\w)"),
]


@register_text_expander("age_phrase")
class AgePhraseExpander:
    name = "age_phrase"

    def expand(self, text: str, ctx: RecognizeContext) -> list[SurfaceSpan]:
        if not text:
            return []
        out: list[SurfaceSpan] = []
        seen: set[tuple[int, int]] = set()
        for pattern in _PATTERNS:
            for m in pattern.finditer(text):
                if m.lastindex:
                    start, end = m.start(1), m.end(1)
                    canonical = m.group(1)
                else:
                    continue
                key = (start, end)
                if key in seen:
                    continue
                seen.add(key)
                try:
                    age = int(canonical)
                except ValueError:
                    continue
                if not (1 <= age <= 120):
                    continue
                out.append(SurfaceSpan(
                    start=start,
                    end=end,
                    surface=text[start:end],
                    canonical=str(age),
                    variant_kind=self.name,
                    entity_hint="AGE",
                    context_boost=True,
                    label="age",
                ))
        return out
