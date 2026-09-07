"""Column-name token normalizers for locale-aware context matching.

Pure functions — no Presidio / GLiNER imports.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Callable

_ARABIC_ALEF = str.maketrans({
    "\u0622": "\u0627",  # ALEF WITH MADDA → ALEF
    "\u0623": "\u0627",  # ALEF WITH HAMZA ABOVE
    "\u0625": "\u0627",  # ALEF WITH HAMZA BELOW
    "\u0671": "\u0627",  # ALEF WASLA
})
_TEH_MARBUTA = str.maketrans({"\u0629": "\u0647"})  # TEH MARBUTA → HEH


def normalize_casefold(text: str) -> str:
    return (text or "").casefold()


def normalize_accent_fold(text: str) -> str:
    """NFKD + strip combining marks so ``téléphone`` ≡ ``telephone``."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_arabic_fold(text: str) -> str:
    """Collapse common Arabic letter variants used in column names."""
    s = text or ""
    s = s.translate(_ARABIC_ALEF)
    s = s.translate(_TEH_MARBUTA)
    # Strip tatweel / diacritics
    s = re.sub(r"[\u064B-\u065F\u0670\u0640]", "", s)
    return s


NORMALIZERS: dict[str, Callable[[str], str]] = {
    "casefold": normalize_casefold,
    "accent_fold": normalize_accent_fold,
    "arabic_fold": normalize_arabic_fold,
    "none": lambda t: t or "",
}

DEFAULT_NORMALIZERS: tuple[str, ...] = ("casefold", "accent_fold", "arabic_fold")


def apply_normalizers(text: str, names: tuple[str, ...] | list[str] | None = None) -> str:
    """Apply named normalizers in order."""
    out = text or ""
    for name in names or DEFAULT_NORMALIZERS:
        fn = NORMALIZERS.get(name)
        if fn is None:
            continue
        out = fn(out)
    return out
