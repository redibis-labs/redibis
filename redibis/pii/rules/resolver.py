"""Span overlap resolution and column verdict resolution."""

from __future__ import annotations

from typing import Optional, Sequence

from redibis.models import PIIDetection
from redibis.pii.scan.result import Candidate, Detection
from redibis.pii.thresholds import Thresholds

# Higher = wins under priority mode
_ENGINE_PRIORITY = {
    "phone": 40,
    "preprocess": 35,  # validated spoken/obfuscated forms
    "regex": 30,
    "ner": 20,
    "llm": 10,
}


def _authority(c: Candidate) -> tuple:
    validated = 1 if c.validator else 0
    return (
        validated,
        _ENGINE_PRIORITY.get(c.engine, 0),
        c.score,
        (c.end or 0) - (c.start or 0),
    )


def _overlaps(a: Candidate, b: Candidate) -> bool:
    if a.start is None or a.end is None or b.start is None or b.end is None:
        return False
    return a.start < b.end and b.start < a.end


class SpanResolver:
    """Merge overlapping candidates into final Detection spans."""

    def resolve(
        self,
        candidates: list[Candidate],
        *,
        min_score: float = 0.35,
        mode: str = "priority",
        entities: tuple[str, ...] = (),
    ) -> tuple[Detection, ...]:
        filtered = [
            c for c in candidates
            if c.score >= min_score
            and c.start is not None
            and c.end is not None
            and c.start < c.end
            and (not entities or c.entity_type in entities
                 or any(c.entity_type == e for e in entities))
        ]
        if entities:
            allowed = set(entities)
            filtered = [c for c in filtered if c.entity_type in allowed]

        if mode == "all":
            chosen = sorted(filtered, key=lambda c: (c.start or 0, -(c.score), c.engine))
            return tuple(self._to_detection(c) for c in chosen)

        # Greedy non-overlapping selection
        ordered = sorted(
            filtered,
            key=lambda c: (
                -((c.end or 0) - (c.start or 0)) if mode == "longest" else 0,
                -_authority(c)[0],
                -_authority(c)[1],
                -_authority(c)[2],
                c.start or 0,
            ),
        )
        selected: list[Candidate] = []
        for c in ordered:
            if any(_overlaps(c, s) for s in selected):
                continue
            selected.append(c)
        selected.sort(key=lambda c: (c.start or 0, -(c.score)))
        return tuple(self._to_detection(c) for c in selected)

    @staticmethod
    def _to_detection(c: Candidate) -> Detection:
        return Detection(
            entity_type=c.entity_type,
            score=c.score,
            engine=c.engine,
            start=c.start,
            end=c.end,
            text=c.text,
            detected=True,
            recognizer=c.recognizer,
            validator=c.validator,
            context_boost=c.context_boost,
            is_proposal=c.is_proposal,
            evidence=(c,),
        )


class VerdictResolver:
    """Apply equation modes to column evidence — wraps ``decide_pii``.

    Detector/ColumnScanner emit ``detected=False``; this resolver sets verdicts.
    """

    def resolve(
        self,
        detections: Sequence[PIIDetection],
        *,
        equation_mode: str = "balanced",
        thresholds: Optional[Thresholds] = None,
    ) -> list[PIIDetection]:
        from redibis.pii.equations import decide_pii

        thr = thresholds or Thresholds()
        return [decide_pii(d, equation_mode, thr) for d in detections]
