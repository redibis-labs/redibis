"""Separator-tolerant digit-cluster expander (spaces, dashes, dots)."""

from __future__ import annotations

import re

from redibis.pii.rules.recognizers import RecognizeContext
from redibis.pii.text_preprocess.expanders._util import fold_indic_digits, label_near
from redibis.pii.text_preprocess.registry import register_text_expander
from redibis.pii.text_preprocess.surface import SurfaceSpan

# Digit groups separated by common separators — at least 8 digits total.
_CLUSTER = re.compile(
    r"(?<!\w)"
    r"\+?[\d٠-٩۰-۹](?:[\d٠-٩۰-۹\s\-–—./_]{6,30}[\d٠-٩۰-۹])"
    r"(?!\w)"
)

_LABELS = {
    "PHONE_NUMBER": [
        "موبايل", "الموبايل", "هاتف", "تليفون", "phone", "mobile", "msisdn",
        "line", "connected",
    ],
    "EG_NATIONAL_ID": ["قومي", "هوية", "national id", "nid", "الرقم القومي"],
    "IMEI": ["imei", "جهاز"],
    "IMSI": ["imsi"],
    "ICCID": ["iccid", "sim", "شريحة"],
    "CREDIT_CARD": [
        "card", "visa", "mastercard", "بطاقة", "ائتمان", "فيزا", "كارت",
    ],
}
# When several labels sit in the same window (e.g. "IMEI بتاع التليفون"),
# prefer the device/id type over the generic phone cue.
_HINT_PRIORITY = (
    "IMEI", "IMSI", "ICCID", "EG_NATIONAL_ID", "CREDIT_CARD", "PHONE_NUMBER",
)


@register_text_expander("digit_cluster")
class DigitClusterExpander:
    name = "digit_cluster"

    def expand(self, text: str, ctx: RecognizeContext) -> list[SurfaceSpan]:
        if not text:
            return []
        out: list[SurfaceSpan] = []
        for m in _CLUSTER.finditer(text):
            surface = m.group(0)
            digits = re.sub(r"\D", "", fold_indic_digits(surface))
            if not (8 <= len(digits) <= 20):
                continue
            hint = ""
            label = ""
            found_hints: list[tuple[str, str]] = []
            for entity, labels in _LABELS.items():
                found = label_near(text, m.start(), m.end(), labels, radius=80)
                if found:
                    found_hints.append((entity, found))
            if found_hints:
                found_hints.sort(
                    key=lambda item: (
                        _HINT_PRIORITY.index(item[0])
                        if item[0] in _HINT_PRIORITY
                        else len(_HINT_PRIORITY)
                    )
                )
                hint, label = found_hints[0]
            # Skip unlabeled contiguous digit runs — regex already covers them.
            # Keep labeled contiguous IMEI / non-EG MSISDN / PAN that validators drop.
            if re.fullmatch(r"[\d٠-٩۰-۹]+", surface) and not hint:
                continue
            if not hint:
                if len(digits) == 14:
                    hint = "EG_NATIONAL_ID"
                elif len(digits) == 15:
                    hint = "IMEI"
                elif digits.startswith("89") and len(digits) >= 18:
                    hint = "ICCID"
                elif 10 <= len(digits) <= 13:
                    hint = "PHONE_NUMBER"
                elif 13 <= len(digits) <= 19:
                    hint = "CREDIT_CARD"
            out.append(SurfaceSpan(
                start=m.start(),
                end=m.end(),
                surface=surface,
                canonical=digits,
                variant_kind=self.name,
                entity_hint=hint,
                context_boost=bool(label),
                label=label,
            ))
        return out
