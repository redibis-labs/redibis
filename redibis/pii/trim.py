"""Shrink a collected span to its value. Never grows. Never empties."""

from __future__ import annotations

from dataclasses import replace
from typing import Optional

from redibis.pii.rules.text_filters import (
    BoundaryNormalizer,
    _NAME_CUE_PREFIX,
    _NO_TRIM_ENTITIES,
)
from redibis.pii.scan.result import Candidate
from redibis.pii.text_preprocess.expanders._util import noise_set, strip_edge_noise
from redibis.pii.text_rules import TextRuleOverlay, default_text_rules


def _overlay(raw) -> TextRuleOverlay:
    if isinstance(raw, TextRuleOverlay):
        return raw
    return default_text_rules()


def trim_span(
    text: str,
    start: int,
    end: int,
    *,
    entity_type: str = "",
    overlay=None,
    policy: str = "edges",
) -> tuple[int, int, list[str]]:
    """Shrink a span to its value: strip edge punctuation, edge noise tokens,
    and leading name/address cue words. Returns ``(start, end, applied_rules)``.

    Never grows. Never returns an empty span (returns the input unchanged
    instead). ``policy`` is reserved; only ``"edges"`` is implemented.
    """
    _ = policy
    n = len(text or "")
    orig_start, orig_end = int(start), int(end)
    if orig_start < 0 or orig_end > n or orig_end <= orig_start:
        return orig_start, orig_end, []
    ov = _overlay(overlay)
    et = str(entity_type or "").upper()
    applied: list[str] = []
    cand = Candidate(
        entity_type=et or "OTHER",
        score=1.0,
        engine="trim",
        start=orig_start,
        end=orig_end,
        text=text[orig_start:orig_end],
    )
    before = (cand.start, cand.end)
    normalized = BoundaryNormalizer(ov).apply([cand], text)
    if not normalized:
        return orig_start, orig_end, []
    cand = normalized[0]
    if (cand.start, cand.end) != before:
        applied.append("edge_punct")
        # Name the stripped noise tokens if the remainder also lost tokens.
        noise = noise_set(overlay=ov)
        ns, ne = strip_edge_noise(text, orig_start, orig_end, noise)
        if (ns, ne) != (orig_start, orig_end) and (ns, ne) == (cand.start, cand.end):
            surface = text[orig_start:orig_end]
            leftover = text[ns:ne]
            stripped = surface.replace(leftover, "", 1).strip() if leftover else surface
            if stripped:
                applied.append(f"edge_noise:{stripped.split()[0]}")
            elif "edge_punct" in applied:
                pass

    if et in {"PERSON", "NAME"} and cand.start is not None and cand.end is not None:
        surface = text[cand.start:cand.end]
        match = _NAME_CUE_PREFIX.match(surface)
        if match:
            nxt = cand.start + match.end()
            if nxt < cand.end and text[nxt:cand.end].strip():
                applied.append(f"name_cue:{match.group(0).strip()}")
                cand = replace(cand, start=nxt, text=text[nxt:cand.end])

    if cand.start is None or cand.end is None or cand.end <= cand.start:
        return orig_start, orig_end, []
    if cand.start < orig_start or cand.end > orig_end:
        return orig_start, orig_end, []
    if et in _NO_TRIM_ENTITIES:
        return orig_start, orig_end, []
    if (cand.start, cand.end) == (orig_start, orig_end):
        return orig_start, orig_end, []
    return int(cand.start), int(cand.end), applied


def trim_spans(
    text: str,
    spans: list[dict],
    *,
    overlay=None,
) -> list[dict]:
    """Return copies of ``spans`` with trimmed offsets. Unchanged spans are copied."""
    out: list[dict] = []
    for raw in spans or ():
        item = dict(raw)
        start, end = int(item.get("start") or 0), int(item.get("end") or 0)
        et = str(item.get("entity_type") or "")
        ns, ne, rules = trim_span(text, start, end, entity_type=et, overlay=overlay)
        item["start"], item["end"] = ns, ne
        if 0 <= ns <= ne <= len(text):
            item["text"] = text[ns:ne]
        if rules:
            item["trimmed"] = True
            item["trim_rules"] = list(rules)
        out.append(item)
    return out
