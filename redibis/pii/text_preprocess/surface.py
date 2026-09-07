"""Immutable surface / canonical span models for free-text preprocessing.

Offsets always refer to the original Unicode surface string. Canonical forms
are internal validation inputs only and never replace ``text[start:end]``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class SurfaceSpan:
    """One obfuscated expression found on the original text surface."""

    start: int
    end: int
    surface: str
    canonical: str
    variant_kind: str
    entity_hint: str = ""
    context_boost: bool = False
    label: str = ""

    def __post_init__(self) -> None:
        if self.start < 0 or self.end < self.start:
            raise ValueError(f"invalid SurfaceSpan offsets: {self.start}:{self.end}")


@dataclass(frozen=True)
class ValidationOutcome:
    """Result of routing a canonical value through a structural validator."""

    ok: bool
    validator: str = ""
    entity_type: str = ""
    score: float = 0.0
    is_proposal: bool = False
    reason: str = ""


@dataclass(frozen=True)
class TextSurface:
    """Immutable wrapper around the user-supplied scan text."""

    text: str

    def slice(self, start: int, end: int) -> str:
        return self.text[start:end]

    def __len__(self) -> int:
        return len(self.text)
