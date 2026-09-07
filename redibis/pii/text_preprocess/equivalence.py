"""Merge equivalent spoken / parenthesized / digit representations into one span."""

from __future__ import annotations

from typing import Optional

from redibis.pii.scan.result import Candidate


def _canonical_of(c: Candidate) -> str:
    # Prefer validator-backed recognizer metadata stashed in recognizer field
    # after pipeline tagging: "kind|canonical".
    rec = c.recognizer or ""
    if "|" in rec:
        return rec.split("|", 1)[1]
    # Fall back to digit-stripped surface text.
    import re
    return re.sub(r"\D", "", c.text or "")


def _near_or_overlap(a: Candidate, b: Candidate, gap: int = 3) -> bool:
    if a.start is None or a.end is None or b.start is None or b.end is None:
        return False
    if a.start < b.end and b.start < a.end:
        return True
    # Adjacent with small punctuation/whitespace gap
    if a.end <= b.start and (b.start - a.end) <= gap:
        return True
    if b.end <= a.start and (a.start - b.end) <= gap:
        return True
    return False


def _compatible_entities(a: str, b: str) -> bool:
    if a == b:
        return True
    pair = {a, b}
    # Spoken NID + parenthetical digits may briefly disagree on type when
    # the label window only covers one of the two surfaces.
    return pair == {"PHONE_NUMBER", "EG_NATIONAL_ID"}


def _preferred_entity(a: Candidate, b: Candidate) -> str:
    types = {a.entity_type, b.entity_type}
    if "EG_NATIONAL_ID" in types:
        return "EG_NATIONAL_ID"
    return a.entity_type if a.score >= b.score else b.entity_type


class VariantEquivalenceMerger:
    """Collapse candidates that share entity + canonical and are near/overlapping."""

    def merge(self, candidates: list[Candidate], text: str = "") -> list[Candidate]:
        preprocess = [c for c in candidates if c.engine == "preprocess"]
        others = [c for c in candidates if c.engine != "preprocess"]
        if not preprocess:
            return candidates

        # Prefer validated non-proposal hits when merging.
        ordered = sorted(
            preprocess,
            key=lambda c: (
                0 if c.is_proposal else 1,
                1 if c.validator else 0,
                c.score,
                (c.end or 0) - (c.start or 0),
            ),
            reverse=True,
        )
        selected: list[Candidate] = []
        for c in ordered:
            merged_into: Optional[Candidate] = None
            for i, s in enumerate(selected):
                if not _compatible_entities(c.entity_type, s.entity_type):
                    continue
                if _canonical_of(c) and _canonical_of(c) == _canonical_of(s) and _near_or_overlap(c, s):
                    start = min(c.start or 0, s.start or 0)
                    end = max(c.end or 0, s.end or 0)
                    surface = text[start:end] if text and 0 <= start < end <= len(text) else (s.text or c.text)
                    canon = _canonical_of(s) or _canonical_of(c)
                    kinds = []
                    for rec in (s.recognizer, c.recognizer):
                        kind = (rec or "").split("|", 1)[0]
                        if kind and kind not in kinds:
                            kinds.append(kind)
                    selected[i] = Candidate(
                        entity_type=_preferred_entity(s, c),
                        score=max(s.score, c.score),
                        engine="preprocess",
                        start=start,
                        end=end,
                        text=surface,
                        recognizer="|".join(kinds + ([canon] if canon else [])),
                        validator=s.validator or c.validator,
                        context_boost=s.context_boost or c.context_boost,
                        is_proposal=s.is_proposal and c.is_proposal,
                    )
                    merged_into = selected[i]
                    break
            if merged_into is None:
                selected.append(c)

        # Also absorb overlapping regex/phone seeds that match the same canonical
        # so SpanResolver receives a single combined span (spoken + parenthetical).
        absorbed: set[int] = set()
        for i, o in enumerate(others):
            for j, s in enumerate(selected):
                if not _compatible_entities(o.entity_type, s.entity_type):
                    continue
                o_canon = _canonical_of(o)
                s_canon = _canonical_of(s)
                if not o_canon or o_canon != s_canon:
                    continue
                if not _near_or_overlap(o, s, gap=4):
                    continue
                start = min(o.start or 0, s.start or 0)
                end = max(o.end or 0, s.end or 0)
                surface = text[start:end] if text and 0 <= start < end <= len(text) else (s.text or o.text)
                selected[j] = Candidate(
                    entity_type=_preferred_entity(s, o),
                    score=max(s.score, o.score),
                    engine="preprocess",
                    start=start,
                    end=end,
                    text=surface,
                    recognizer=s.recognizer,
                    validator=s.validator or o.validator,
                    context_boost=s.context_boost or o.context_boost,
                    is_proposal=False if (s.validator or o.validator) else (s.is_proposal and o.is_proposal),
                )
                absorbed.add(i)
                break

        kept_others = [o for i, o in enumerate(others) if i not in absorbed]
        return selected + kept_others