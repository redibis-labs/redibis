"""Post-recognizer filters compiled from ``TextRuleOverlay``.

Applied after engines (and spoken-digit equivalence merge) and before
``SpanResolver``. Deterministic — no model calls.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Iterable, Optional, Sequence

from redibis.pii.scan.result import Candidate
from redibis.pii.text_preprocess.expanders._util import (
    folded_without_noise,
    fold_ar,
    noise_set,
    strip_edge_noise,
    token_fold,
    tokenize_with_spans,
)
from redibis.pii.text_rules import TextRuleOverlay, default_text_rules

_SPEAKER_TURN = re.compile(
    r"(?:^|[\n\r])\s*(?:Agent|Caller|الوكيل|المتصل)\s*:",
    re.IGNORECASE,
)
_TRAIL_PUNCT = re.compile(r"[\s.،,;:!?؟»\"']+$")
_TRAIL_FILLER = re.compile(r"(?:\s+(?:صح|right|yes))+$", re.IGNORECASE)
_LEAD_SEP = re.compile(r"^[\s:：=\-–—]+")
_EDGE_TRIM = re.compile(r"^[\s:：=\-–—]+|[\s.،,;:!?؟…»«\"']+$")
_NO_TRIM_ENTITIES = frozenset({"EMAIL_ADDRESS", "URL", "IP_ADDRESS", "MAC_ADDRESS"})
_LEAD_STRIP = " \t\r\n:：=-–—"
_NAME_CUE_PREFIX = re.compile(
    r"^(?:واسم المفوض|اسم المفوض|اسمي|واسمي|باسم|اسمها|اسمه|معاك|معك|it['’]s|it is|i am|i['’]m|name is)\s+",
    re.IGNORECASE,
)
_ADDRESS_HEAD = re.compile(
    r"(?i)(?:\d+|شارع|(?<!عن )طريق|عمارة|شقة|حي|مدينة|قدام|واحة|الكيلو|"
    r"villa|street|st\.?|building|apt\.?|apartment|flat)"
)
_CANON_DIGITS = re.compile(r"\D+")
_SENTENCE_STOPS = (".", "؟", "!", "\n")


def next_sentence_break(text: str, start: int) -> int:
    """Index of the next sentence stop at or after ``start``, or ``len(text)``.

    Same stop set as ``ContextSpanExtender._clause_after``: ``.`` ``؟`` ``!``
    newline, plus a speaker-turn boundary.
    """
    if start >= len(text):
        return len(text)
    speaker = _SPEAKER_TURN.search(text, start)
    end = speaker.start() if speaker else len(text)
    stops = [p for p in (text.find(sep, start) for sep in _SENTENCE_STOPS) if p >= 0]
    if stops:
        stop = min(stops)
        if stop < end:
            end = stop
    return end


def prev_sentence_break(text: str, pos: int) -> int:
    """Start of the sentence containing ``pos`` (after the previous stop)."""
    if pos <= 0:
        return 0
    lo = 0
    for sep in _SENTENCE_STOPS:
        p = text.rfind(sep, 0, pos)
        if p >= lo:
            lo = p + 1
    for match in _SPEAKER_TURN.finditer(text[:pos]):
        if match.end() > lo:
            lo = match.end()
    while lo < pos and lo < len(text) and text[lo] in " \t":
        lo += 1
    return lo


def enclosing_sentence(text: str, start: int, end: int) -> tuple[int, int]:
    """Return the enclosing sentence ``[lo, hi)`` covering ``[start, end)``."""
    start = max(0, min(start, len(text)))
    end = max(start, min(end, len(text)))
    lo = prev_sentence_break(text, start)
    hi = next_sentence_break(text, end if end < len(text) else max(0, len(text) - 1))
    if hi < end:
        hi = end
    if lo > start:
        lo = start
    return lo, max(hi, end)


def _overlay(rules: Optional[TextRuleOverlay]) -> TextRuleOverlay:
    return rules if isinstance(rules, TextRuleOverlay) else default_text_rules()


def _fold_surface(text: str) -> str:
    cleaned = _LEAD_SEP.sub("", (text or "").strip())
    cleaned = _TRAIL_PUNCT.sub("", cleaned)
    return fold_ar(cleaned)


def _canonical_digits(value: str) -> str:
    return _CANON_DIGITS.sub("", value or "")


class TermExclusionFilter:
    """Drop spans whose folded surface is an excluded role / filler word.

    After noise tokens are removed from the surface, an empty remainder or an
    exact exclude-term match is dropped. A filler glued inside a longer span
    is not an exclude-term match — that is ``noise_terms``'s job.
    """

    def __init__(self, overlay: Optional[TextRuleOverlay] = None):
        ov = _overlay(overlay)
        self._terms = {_fold_surface(t) for t in ov.exclude_terms if t}
        self._noise = noise_set(overlay=ov)
        self._patterns = []
        for raw in ov.exclude_patterns:
            try:
                self._patterns.append(re.compile(raw))
            except re.error:
                continue

    def apply(self, candidates: Sequence[Candidate], text: str = "") -> list[Candidate]:
        out: list[Candidate] = []
        for cand in candidates:
            surface = cand.text if cand.text is not None else (
                text[cand.start:cand.end]
                if cand.start is not None and cand.end is not None
                else ""
            )
            folded = _fold_surface(surface)
            stripped = folded_without_noise(surface, self._noise)
            if not stripped or stripped in self._terms or (folded and folded in self._terms):
                continue
            if any(p.search(surface or "") or p.search(folded) or p.search(stripped) for p in self._patterns):
                continue
            out.append(cand)
        return out


class ContextSpanExtender:
    """Grow keyword LOCATION (and similar) hits to the surrounding clause."""

    def __init__(self, overlay: Optional[TextRuleOverlay] = None):
        self._overlay = _overlay(overlay)

    def apply(self, candidates: Sequence[Candidate], text: str) -> list[Candidate]:
        if not text:
            return list(candidates)
        out = list(candidates)
        out = self._trim_name_cues(out, text)
        for entity, cue in self._overlay.context_cues.items():
            if (cue.extend or "").lower() != "sentence" or not cue.triggers:
                continue
            out = self._extend_entity(out, text, entity, cue.triggers)
        return out

    @staticmethod
    def _trim_name_cues(candidates: Sequence[Candidate], text: str) -> list[Candidate]:
        out: list[Candidate] = []
        for cand in candidates:
            if cand.entity_type not in {"PERSON", "NAME"}:
                out.append(cand)
                continue
            if cand.start is None or cand.end is None:
                out.append(cand)
                continue
            surface = text[cand.start:cand.end]
            m = _NAME_CUE_PREFIX.match(surface)
            if not m:
                out.append(cand)
                continue
            start = cand.start + m.end()
            if start >= cand.end:
                out.append(cand)
                continue
            slice_text = text[start:cand.end]
            out.append(replace(
                cand,
                start=start,
                text=slice_text,
                context_boost=True,
                score=max(cand.score, 0.8),
            ))
        return out

    def _extend_entity(
        self,
        candidates: list[Candidate],
        text: str,
        entity: str,
        triggers: Sequence[str],
    ) -> list[Candidate]:
        clauses = self._find_clauses(text, triggers)
        if not clauses:
            return candidates
        kept: list[Candidate] = []
        covered = [False] * len(clauses)
        for cand in candidates:
            if cand.entity_type not in {entity, "ADDRESS", "LOCATION"} and entity == "LOCATION":
                kept.append(cand)
                continue
            if entity != "LOCATION" and cand.entity_type != entity:
                kept.append(cand)
                continue
            if cand.start is None or cand.end is None:
                kept.append(cand)
                continue
            absorbed = False
            for i, (cs, ce) in enumerate(clauses):
                if cand.start < ce and cand.end > cs:
                    covered[i] = True
                    absorbed = True
                    break
            if not absorbed:
                kept.append(cand)
        for i, (cs, ce) in enumerate(clauses):
            if ce <= cs:
                continue
            kept.append(Candidate(
                entity_type=entity if entity != "ADDRESS" else "LOCATION",
                score=0.86,
                engine="regex",
                start=cs,
                end=ce,
                text=text[cs:ce],
                recognizer="context_span_extender",
                context_boost=True,
            ))
        return kept

    def _find_clauses(self, text: str, triggers: Sequence[str]) -> list[tuple[int, int]]:
        clauses: list[tuple[int, int]] = []
        for trig in triggers:
            if not trig:
                continue
            for match in re.finditer(re.escape(trig), text, flags=re.IGNORECASE):
                clause = self._clause_after(text, match.end())
                if clause:
                    clauses.append(clause)
        return _merge_ranges(clauses)

    @staticmethod
    def _clause_after(text: str, after: int) -> Optional[tuple[int, int]]:
        start = after
        while start < len(text) and text[start] in " \t:：=-–—":
            start += 1
        if start >= len(text):
            return None
        end = next_sentence_break(text, start)
        remainder = text[start:end]
        head = _ADDRESS_HEAD.search(remainder)
        if not head:
            return None
        clause_start = start + head.start()
        clause = remainder[head.start():]
        trimmed = _TRAIL_PUNCT.sub("", clause)
        trimmed = _TRAIL_FILLER.sub("", trimmed)
        # Also strip operator noise-lexicon tokens from the edges.
        noise = noise_set()
        ts, te = strip_edge_noise(trimmed, 0, len(trimmed), noise)
        trimmed = trimmed[ts:te]
        trimmed = _TRAIL_PUNCT.sub("", trimmed)
        if not trimmed.strip():
            return None
        return clause_start, clause_start + len(trimmed)


class NumberContextClassifier:
    """Keep, relabel, or drop numeric candidates using overlay cues + units."""

    _PHONE_PREFIXES = ("010", "011", "012", "015", "20")
    _NUMERIC_ENTS = frozenset({
        "PHONE_NUMBER", "PHONE", "MSISDN", "EG_NATIONAL_ID", "NATIONAL_ID",
        "CREDIT_CARD", "IMEI", "IMSI", "ICCID", "OTP", "SIM_PUK", "VOUCHER",
        "SUPPORT_TICKET", "AGE", "LOCATION",
    })

    def __init__(self, overlay: Optional[TextRuleOverlay] = None):
        self._overlay = _overlay(overlay)
        units = [fold_ar(u) for u in self._overlay.quantity_units if u]
        self._unit_re = None
        if units:
            body = "|".join(re.escape(u) for u in sorted(units, key=len, reverse=True))
            self._unit_re = re.compile(
                rf"(?i)(?:^|[\s\b])(?:\d+[\d\s]*)\s*(?:{body})\b"
                rf"|(?:{body})\s*$"
            )
        self._cues: list[tuple[str, str]] = []
        for et, cue in self._overlay.context_cues.items():
            for trig in cue.triggers:
                self._cues.append((et, fold_ar(trig)))

    def apply(self, candidates: Sequence[Candidate], text: str) -> list[Candidate]:
        location_ranges = [
            (c.start, c.end)
            for c in candidates
            if c.entity_type in {"LOCATION", "ADDRESS"}
            and c.start is not None and c.end is not None
        ]
        out: list[Candidate] = []
        for cand in candidates:
            if cand.start is None or cand.end is None:
                out.append(cand)
                continue
            surface = text[cand.start:cand.end]
            if self._is_quantity(text, cand, surface):
                continue
            if self._inside_address_house_number(cand, surface, location_ranges):
                continue
            relabeled = self._reclassify(text, cand, surface)
            out.append(relabeled)
        return out

    def _is_quantity(self, text: str, cand: Candidate, surface: str) -> bool:
        if self._unit_re is None:
            return False
        window = text[max(0, cand.start - 4): min(len(text), cand.end + 16)]
        if self._unit_re.search(fold_ar(window)) or self._unit_re.search(window):
            digits = _canonical_digits(surface)
            if 0 < len(digits) <= 6:
                return True
        after = fold_ar(text[cand.end: min(len(text), cand.end + 12)].strip())
        for unit in self._overlay.quantity_units:
            if after.startswith(fold_ar(unit)):
                return True
        return False

    @staticmethod
    def _inside_address_house_number(
        cand: Candidate,
        surface: str,
        locations: list[tuple[int, int]],
    ) -> bool:
        if cand.entity_type in {"LOCATION", "ADDRESS"}:
            return False
        digits = _canonical_digits(surface)
        if not digits or len(digits) > 4:
            return False
        if cand.entity_type not in {
            "PHONE_NUMBER", "EG_NATIONAL_ID", "CREDIT_CARD", "OTP", "AGE",
        } and not digits.isdigit():
            return False
        for start, end in locations:
            if cand.start >= start and cand.end <= end:
                return True
        return False

    def _reclassify(self, text: str, cand: Candidate, surface: str) -> Candidate:
        if cand.entity_type in {
            "IP_ADDRESS", "EMAIL_ADDRESS", "PASSPORT", "EG_TAX_ID",
            "ORGANIZATION", "GDPR_SPECIAL_CATEGORY", "LOCATION", "PERSON",
            "CVV", "CREDIT_CARD_EXPIRATION", "IMEI",
        }:
            return cand
        digits = _canonical_digits(surface)
        if cand.recognizer and "|" in cand.recognizer:
            _, _, tail = cand.recognizer.partition("|")
            if tail and tail[0:1].isdigit():
                digits = _canonical_digits(tail) or digits
        nearest = self._nearest_cue(text, cand.start, cand.end)
        n = len(digits)
        target = cand.entity_type
        window = fold_ar(text[max(0, cand.start - 220): min(len(text), cand.end + 20)])
        voucherish = any(
            fold_ar(tok) in window
            for tok in ("scratch", "scratch card", "كارت شحن", "14 رقم", "كود الشحن")
        )
        egypt_msisdn = n == 11 and digits.startswith(("010", "011", "012", "015"))
        if egypt_msisdn:
            target = "PHONE_NUMBER"
        elif (nearest == "VOUCHER" or voucherish) and n == 14:
            target = "VOUCHER"
        elif nearest == "SIM_PUK" and n == 8:
            target = "SIM_PUK"
        elif nearest == "EG_NATIONAL_ID" and 12 <= n <= 15:
            target = "EG_NATIONAL_ID"
        elif nearest == "PHONE_NUMBER" and 8 <= n <= 13:
            target = "PHONE_NUMBER"
        elif nearest == "SUPPORT_TICKET" and (
            bool(digits) or "SR-" in surface.upper() or cand.entity_type == "SUPPORT_TICKET"
        ):
            if cand.entity_type in {"PERSON", "LOCATION", "EMAIL_ADDRESS"}:
                pass
            else:
                target = "SUPPORT_TICKET"
        elif n == 14 and nearest not in {"EG_NATIONAL_ID", "VOUCHER"}:
            pass
        elif n == 11 and digits.startswith(self._PHONE_PREFIXES):
            if cand.entity_type in self._NUMERIC_ENTS or cand.engine == "preprocess":
                target = "PHONE_NUMBER"
        if target == cand.entity_type:
            return cand
        return replace(
            cand,
            entity_type=target,
            context_boost=True,
            score=max(cand.score, 0.8),
        )

    def _nearest_cue(self, text: str, start: int, end: int) -> str:
        window = fold_ar(text[max(0, start - 160): min(len(text), end + 40)])
        best = ""
        best_key = (-1, -10**9)  # longer trigger wins, then closer
        for et, trig in self._cues:
            if not trig:
                continue
            pos = window.rfind(trig)
            if pos < 0:
                continue
            dist = len(window) - pos
            key = (len(trig), -dist)
            if key > best_key:
                best_key = key
                best = et
        return best


class BoundaryNormalizer:
    """Trim edge punctuation/whitespace from every span, per entity family.

    Runs after all producers so it fixes regex, NER and preprocess spans alike.
    Entity types whose value legitimately ends in punctuation are exempt.
    After punctuation trimming, whole leading/trailing noise tokens are
    stripped until stable.
    """

    name = "boundary_normalize"

    def __init__(self, overlay: Optional[TextRuleOverlay] = None):
        self._noise = noise_set(overlay=overlay) if overlay is not None else noise_set()

    def apply(self, candidates: Sequence[Candidate], text: str) -> list[Candidate]:
        out: list[Candidate] = []
        for cand in candidates:
            if cand.start is None or cand.end is None or cand.entity_type in _NO_TRIM_ENTITIES:
                out.append(cand)
                continue
            surface = text[cand.start:cand.end]
            lead = len(surface) - len(surface.lstrip(_LEAD_STRIP))
            trail = len(surface) - len(_EDGE_TRIM.sub("", surface[lead:])) - lead
            start, end = cand.start + lead, cand.end - trail
            if end <= start:
                continue
            start, end = strip_edge_noise(text, start, end, self._noise)
            if end <= start:
                continue
            if (start, end) == (cand.start, cand.end):
                out.append(cand)
                continue
            out.append(replace(cand, start=start, end=end, text=text[start:end]))
        return out


def apply_text_rule_filters(
    candidates: Iterable[Candidate],
    text: str,
    overlay: Optional[TextRuleOverlay],
) -> list[Candidate]:
    """Extender → number policy → exclusions → boundary trim."""
    ov = _overlay(overlay)
    stage = list(candidates)
    stage = ContextSpanExtender(ov).apply(stage, text)
    stage = NumberContextClassifier(ov).apply(stage, text)
    stage = TermExclusionFilter(ov).apply(stage, text)
    stage = BoundaryNormalizer(ov).apply(stage, text)
    return stage


def _merge_ranges(ranges: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    if not ranges:
        return []
    ordered = sorted((a, b) for a, b in ranges if b > a)
    merged: list[list[int]] = []
    for start, end in ordered:
        if not merged or start > merged[-1][1] + 1:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(a, b) for a, b in merged]
