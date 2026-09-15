"""Shared helpers for free-text expanders."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from redibis.pii.token_normalize import apply_normalizers, normalize_arabic_fold

_ARABIC_INDIC = str.maketrans(
    "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹",
    "01234567890123456789",
)

_LEXICON_DIR = Path(__file__).resolve().parent.parent / "lexicons"


def fold_ar(text: str) -> str:
    return apply_normalizers(text or "", ("casefold", "arabic_fold"))


def fold_indic_digits(text: str) -> str:
    return (text or "").translate(_ARABIC_INDIC)


@lru_cache(maxsize=4)
def load_yaml_lexicon(name: str) -> dict[str, Any]:
    path = _LEXICON_DIR / name
    if not path.exists():
        return {}
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def context_window(text: str, start: int, end: int, radius: int = 40) -> str:
    left = max(0, start - radius)
    right = min(len(text), end + radius)
    return text[left:right]


def label_near(text: str, start: int, end: int, labels: list[str], radius: int = 50) -> str:
    window = fold_ar(context_window(text, start, end, radius))
    for label in labels:
        if fold_ar(label) in window:
            return label
    return ""


_TOKEN_SPLIT = re.compile(r"[\s,،/\\|;:]+")
_EDGE_PUNCT = re.compile(r"^[\(\[\{«\"'،,.;:!?؟]+|[\)\]\}»\"'،,.;:!?؟]+$")


def tokenize_with_spans(text: str) -> list[tuple[str, int, int]]:
    """Return (token, start, end) for whitespace/punctuation-separated tokens."""
    out: list[tuple[str, int, int]] = []
    for m in re.finditer(r"\S+", text):
        out.append((m.group(0), m.start(), m.end()))
    return out


def token_fold(tok: str) -> str:
    """Fold a token the same way spoken-digit matching does."""
    return fold_ar(_EDGE_PUNCT.sub("", tok or ""))


@lru_cache(maxsize=64)
def _noise_set_cached(terms: tuple[str, ...]) -> frozenset[str]:
    return frozenset(token_fold(t) for t in terms if t and token_fold(t))


def noise_set(ctx=None, *, overlay=None) -> frozenset[str]:
    """Folded noise tokens from the overlay on ``ctx``, or the shipped default.

    Cached per overlay term-tuple. Called inside token loops.
    """
    rules = overlay
    if rules is None:
        rules = getattr(ctx, "text_rules", None) if ctx is not None else None
    if rules is None:
        from redibis.pii.text_rules import default_text_rules

        terms = default_text_rules().noise_terms
    else:
        terms = tuple(getattr(rules, "noise_terms", ()) or ())
    return _noise_set_cached(terms)


def strip_edge_noise(
    text: str,
    start: int,
    end: int,
    noise: frozenset[str],
) -> tuple[int, int]:
    """Trim whole leading/trailing noise tokens until stable. Offsets into ``text``."""
    if not text or end <= start or not noise:
        return start, end
    changed = True
    while changed and end > start:
        changed = False
        surface = text[start:end]
        tokens = tokenize_with_spans(surface)
        if not tokens:
            break
        first_tok, first_rel_s, first_rel_e = tokens[0]
        if token_fold(first_tok) in noise:
            start = start + first_rel_e
            while start < end and text[start].isspace():
                start += 1
            changed = True
            continue
        last_tok, last_rel_s, _last_rel_e = tokens[-1]
        if token_fold(last_tok) in noise:
            end = start + last_rel_s
            while end > start and text[end - 1].isspace():
                end -= 1
            changed = True
    return start, end


def folded_without_noise(surface: str, noise: frozenset[str]) -> str:
    """Folded surface after dropping noise tokens (used for exclusion matching)."""
    if not surface:
        return ""
    kept: list[str] = []
    for tok, _s, _e in tokenize_with_spans(surface):
        key = token_fold(tok)
        if key and key not in noise:
            kept.append(key)
    if not kept:
        return ""
    return fold_ar(" ".join(kept))
