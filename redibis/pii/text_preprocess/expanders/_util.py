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


def tokenize_with_spans(text: str) -> list[tuple[str, int, int]]:
    """Return (token, start, end) for whitespace/punctuation-separated tokens."""
    out: list[tuple[str, int, int]] = []
    for m in re.finditer(r"\S+", text):
        out.append((m.group(0), m.start(), m.end()))
    return out
