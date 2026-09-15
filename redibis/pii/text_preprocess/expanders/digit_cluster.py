"""Separator-tolerant digit-cluster expander (spaces, dashes, dots)."""

from __future__ import annotations

import re

from redibis.pii.rules.recognizers import RecognizeContext
from redibis.pii.text_preprocess.expanders._util import (
    fold_indic_digits,
    label_near,
    noise_set,
    token_fold,
    tokenize_with_spans,
)
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
        seen: set[tuple[int, int]] = set()
        for span in self._expand_regex(text, ctx):
            key = (span.start, span.end)
            if key in seen:
                continue
            seen.add(key)
            out.append(span)
        for span in self._expand_skipping_noise(text, ctx):
            key = (span.start, span.end)
            if key in seen:
                continue
            # Drop if an existing regex span already covers the same digits.
            if any(s.start <= span.start and s.end >= span.end for s in out):
                continue
            seen.add(key)
            out.append(span)
        return out

    def _hint_for(self, text: str, start: int, end: int, digits: str) -> tuple[str, str]:
        hint = ""
        label = ""
        found_hints: list[tuple[str, str]] = []
        for entity, labels in _LABELS.items():
            found = label_near(text, start, end, labels, radius=80)
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
        return hint, label

    def _expand_regex(self, text: str, ctx: RecognizeContext) -> list[SurfaceSpan]:
        out: list[SurfaceSpan] = []
        for m in _CLUSTER.finditer(text):
            surface = m.group(0)
            digits = re.sub(r"\D", "", fold_indic_digits(surface))
            if not (8 <= len(digits) <= 20):
                continue
            hint, label = self._hint_for(text, m.start(), m.end(), digits)
            # Skip unlabeled contiguous digit runs — regex already covers them.
            # Keep labeled contiguous IMEI / non-EG MSISDN / PAN that validators drop.
            if re.fullmatch(r"[\d٠-٩۰-۹]+", surface) and not hint:
                continue
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

    def _expand_skipping_noise(self, text: str, ctx: RecognizeContext) -> list[SurfaceSpan]:
        """Walk tokens, skipping noise, so fillers between digits do not break a cluster."""
        noise = noise_set(ctx)
        if not noise:
            return []
        tokens = tokenize_with_spans(text)
        seps = {"-", "–", "—", ".", "/", "_"}
        out: list[SurfaceSpan] = []
        i = 0
        while i < len(tokens):
            tok, ts, te = tokens[i]
            folded = token_fold(tok)
            if folded in noise:
                i += 1
                continue
            digits_here = re.sub(r"\D", "", fold_indic_digits(tok))
            if not digits_here:
                i += 1
                continue
            first_start = ts
            last_end = te
            collected = [digits_here]
            j = i + 1
            consumed = 1
            while j < len(tokens):
                ntok, nts, nte = tokens[j]
                nfold = token_fold(ntok)
                if nfold in noise:
                    j += 1
                    consumed += 1
                    continue
                ndigits = re.sub(r"\D", "", fold_indic_digits(ntok))
                stripped = ntok.strip()
                if ndigits:
                    last_end = nte
                    collected.append(ndigits)
                    j += 1
                    consumed += 1
                    continue
                if stripped in seps or nfold in seps:
                    last_end = nte
                    j += 1
                    consumed += 1
                    continue
                break
            digits = "".join(collected)
            if 8 <= len(digits) <= 20 and last_end > first_start:
                surface = text[first_start:last_end]
                # Only emit when a noise token actually sat inside the walk —
                # regex already covers clean clusters.
                inner = text[tokens[i][2]:last_end]
                had_noise = any(token_fold(t) in noise for t, _, _ in tokenize_with_spans(inner))
                if had_noise:
                    hint, label = self._hint_for(text, first_start, last_end, digits)
                    out.append(SurfaceSpan(
                        start=first_start,
                        end=last_end,
                        surface=surface,
                        canonical=digits,
                        variant_kind=self.name,
                        entity_hint=hint,
                        context_boost=bool(label),
                        label=label,
                    ))
            i += max(consumed, 1)
        return out
