"""Split long free-text into overlapping windows for sequence models.

GLiNER (and similar) silently truncates at a few hundred tokens. Regex and
Presidio already walk the whole string; NER must too. Offsets are Unicode
code points into the original text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable

_SPEAKER_TURN = re.compile(
    r"(?:^|[\n\r])\s*(?:Agent|Caller|الوكيل|المتصل)\s*:",
    re.IGNORECASE,
)
_SENTENCE = re.compile(r"[.؟!!\n،]")
_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class Window:
    start: int  # code-point offset into the ORIGINAL text
    end: int
    text: str


def windows(
    text: str,
    *,
    target_chars: int = 1200,
    overlap_chars: int = 200,
) -> list[Window]:
    """Split on sentence/speaker-turn boundaries, then whitespace, then a hard cut.

    Windows overlap so an entity on a seam is still seen whole.
    """
    if not text:
        return []
    target = max(32, int(target_chars or 1200))
    overlap = max(0, min(int(overlap_chars or 0), target - 1))
    if len(text) <= target:
        return [Window(0, len(text), text)]

    cuts = _boundary_cuts(text)
    out: list[Window] = []
    pos = 0
    n = len(text)
    while pos < n:
        ideal_end = min(n, pos + target)
        end = _snap_end(text, pos, ideal_end, cuts)
        if end <= pos:
            end = min(n, pos + target)
        out.append(Window(pos, end, text[pos:end]))
        if end >= n:
            break
        next_pos = end - overlap
        if next_pos <= pos:
            next_pos = end
        pos = next_pos
    return out


def map_windows(
    text: str,
    predict: Callable[[str], Iterable[Any]],
    *,
    target_chars: int = 1200,
    overlap_chars: int = 200,
    max_windows: int = 200,
) -> tuple[list[Any], dict[str, Any]]:
    """Run ``predict`` on each window, rebase offsets, de-dupe overlaps.

    ``predict`` receives the window text and returns NERSpan-like objects or
    dicts with ``start`` / ``end`` / ``label`` / ``score`` relative to the window.
    """
    wins = windows(text, target_chars=target_chars, overlap_chars=overlap_chars)
    total = len(wins)
    capped = False
    cap = max(1, int(max_windows or 200))
    if len(wins) > cap:
        wins = wins[:cap]
        capped = True

    merged: dict[tuple[int, int, str], Any] = {}
    covered_end = 0
    for win in wins:
        try:
            raw = list(predict(win.text) or [])
        except Exception:
            raw = []
        covered_end = max(covered_end, win.end)
        for span in raw:
            rebased = _rebase(span, win, text)
            if rebased is None:
                continue
            start, end, label, score, obj = rebased
            key = (start, end, label)
            prior = merged.get(key)
            if prior is None or score > prior[0]:
                merged[key] = (score, obj)

    fraction = (covered_end / len(text)) if text else 1.0
    reasons: dict[str, str] = {}
    if capped:
        reasons["ner"] = f"window cap reached ({cap})"
    coverage = {
        "windows_scanned": len(wins),
        "windows_total": total,
        "fraction": round(fraction, 4),
        "covered_end": covered_end,
        "reasons": reasons,
    }
    return [item[1] for item in merged.values()], coverage


def _boundary_cuts(text: str) -> list[int]:
    cuts = {0, len(text)}
    for m in _SPEAKER_TURN.finditer(text):
        cuts.add(m.start())
        cuts.add(m.end())
    for m in _SENTENCE.finditer(text):
        cuts.add(m.end())
    for m in _WS.finditer(text):
        cuts.add(m.end())
    return sorted(c for c in cuts if 0 <= c <= len(text))


def _snap_end(text: str, start: int, ideal_end: int, cuts: list[int]) -> int:
    if ideal_end >= len(text):
        return len(text)
    # Prefer a boundary in the last third of the window.
    lo = start + max(1, (ideal_end - start) // 3)
    best = None
    for cut in cuts:
        if lo <= cut <= ideal_end:
            best = cut
        elif cut > ideal_end:
            break
    if best is not None:
        return best
    # Whitespace fallback already in cuts; last resort hard cut.
    return ideal_end


def _rebase(span: Any, win: Window, original: str) -> tuple[int, int, str, float, Any] | None:
    if hasattr(span, "start"):
        start = int(span.start) + win.start
        end = int(span.end) + win.start
        label = str(getattr(span, "label", "") or "")
        score = float(getattr(span, "score", 0) or 0)
        text = original[start:end] if 0 <= start < end <= len(original) else ""
        from dataclasses import replace

        try:
            obj = replace(span, start=start, end=end, text=text)
        except TypeError:
            obj = span
            try:
                object.__setattr__(span, "start", start)
                object.__setattr__(span, "end", end)
                if hasattr(span, "text"):
                    object.__setattr__(span, "text", text)
            except Exception:
                obj = {
                    "start": start,
                    "end": end,
                    "label": label,
                    "score": score,
                    "text": text,
                    "model": getattr(span, "model", ""),
                }
        return start, end, label, score, obj
    if isinstance(span, dict):
        try:
            start = int(span.get("start", -1)) + win.start
            end = int(span.get("end", -1)) + win.start
        except (TypeError, ValueError):
            return None
        label = str(span.get("label") or "")
        score = float(span.get("score") or 0)
        out = dict(span)
        out["start"] = start
        out["end"] = end
        if 0 <= start < end <= len(original):
            out["text"] = original[start:end]
        return start, end, label, score, out
    return None
