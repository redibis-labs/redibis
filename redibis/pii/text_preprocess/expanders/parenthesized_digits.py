"""Parenthesized / grouped digit expander: (0)(1)(1)... or (01143215567)."""

from __future__ import annotations

import re

from redibis.pii.rules.recognizers import RecognizeContext
from redibis.pii.text_preprocess.expanders._util import fold_indic_digits, label_near
from redibis.pii.text_preprocess.registry import register_text_expander
from redibis.pii.text_preprocess.surface import SurfaceSpan

# Single parenthesized digit run OR a sequence of single-digit groups.
_GROUPED = re.compile(
    r"(?<!\w)"
    r"(?:"
    r"\(\s*[\d٠-٩۰-۹]{1,4}\s*\)(?:\s*\(\s*[\d٠-٩۰-۹]{1,4}\s*\)){2,}"
    r"|"
    r"\(\s*[\d٠-٩۰-۹]{8,20}\s*\)"
    r")"
    r"(?!\w)"
)

_PHONE_LABELS = ["موبايل", "هاتف", "تليفون", "phone", "mobile", "msisdn", "رقم"]
_NID_LABELS = ["قومي", "هوية", "national id", "nid", "الرقم القومي"]


@register_text_expander("parenthesized_digits")
class ParenthesizedDigitsExpander:
    name = "parenthesized_digits"

    def expand(self, text: str, ctx: RecognizeContext) -> list[SurfaceSpan]:
        if not text:
            return []
        out: list[SurfaceSpan] = []
        for m in _GROUPED.finditer(text):
            surface = m.group(0)
            digits = re.sub(r"\D", "", fold_indic_digits(surface))
            if len(digits) < 8:
                continue
            phone_label = label_near(text, m.start(), m.end(), _PHONE_LABELS)
            # Spoken Arabic NIDs sit between the label and the parenthetical
            # digits — use a wider window than the default phone radius.
            nid_label = label_near(text, m.start(), m.end(), _NID_LABELS, radius=120)
            hint = ""
            if nid_label or (len(digits) in (13, 14) and digits[:1] in "23"):
                hint = "EG_NATIONAL_ID"
            elif phone_label or 10 <= len(digits) <= 13:
                hint = "PHONE_NUMBER"
            out.append(SurfaceSpan(
                start=m.start(),
                end=m.end(),
                surface=surface,
                canonical=digits,
                variant_kind=self.name,
                entity_hint=hint,
                context_boost=bool(phone_label or nid_label),
                label=phone_label or nid_label,
            ))
        return out
