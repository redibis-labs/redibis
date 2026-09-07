"""Label-bound secret / password / API-key / OTP expander."""

from __future__ import annotations

import re

from redibis.pii.rules.recognizers import RecognizeContext
from redibis.pii.text_preprocess.registry import register_text_expander
from redibis.pii.text_preprocess.surface import SurfaceSpan

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "PASSWORD_HASH",
        re.compile(
            r"(?i)(?<!\w)(?:password|passwd|pwd|pass|كلمة\s*المرور|الباسورد)\s*[:=]\s*([^\s]{4,64})"
        ),
    ),
    (
        "API_KEY",
        re.compile(
            r"(?i)(?<!\w)(?:api[_-]?key|secret[_-]?key|access[_-]?token|bearer)\s*[:=]\s*([A-Za-z0-9_\-]{8,128})"
        ),
    ),
    (
        "SECRET",
        re.compile(
            r"(?i)(?<!\w)(?:secret|token)\s*[:=]\s*([A-Za-z0-9_\-./+=]{8,128})"
        ),
    ),
    (
        "OTP",
        re.compile(
            r"(?i)(?<!\w)(?:otp|كود|رمز(?:\s*التحقق)?)\s*[:=]?\s*(\d{4,8})(?!\w)"
        ),
    ),
    (
        "SIM_PUK",
        re.compile(
            r"(?i)(?<!\w)(?:puk|رمز\s*الـ?\s*puk)\s*[:=]?\s*(\d{8})(?!\w)"
        ),
    ),
]


@register_text_expander("labeled_secret")
class LabeledSecretExpander:
    name = "labeled_secret"

    def expand(self, text: str, ctx: RecognizeContext) -> list[SurfaceSpan]:
        if not text:
            return []
        out: list[SurfaceSpan] = []
        for entity, pattern in _PATTERNS:
            for m in pattern.finditer(text):
                # Prefer capturing the secret value only when group 1 exists;
                # span the full match so de-id also covers the label if desired.
                # Use the value group for canonical / offsets of the secret itself.
                if m.lastindex:
                    start, end = m.start(1), m.end(1)
                    canonical = m.group(1)
                else:
                    start, end = m.start(), m.end()
                    canonical = m.group(0)
                out.append(SurfaceSpan(
                    start=start,
                    end=end,
                    surface=text[start:end],
                    canonical=canonical,
                    variant_kind=self.name,
                    entity_hint=entity,
                    context_boost=True,
                    label=entity.lower(),
                ))
        return out
