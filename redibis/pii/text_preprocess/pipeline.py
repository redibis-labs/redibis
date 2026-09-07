"""Obfuscation pipeline: expand → validate → Candidate (original offsets)."""

from __future__ import annotations

import logging
from typing import Optional, Sequence

from redibis.pii.rules.recognizers import RecognizeContext
from redibis.pii.scan.result import Candidate
from redibis.pii.text_preprocess.equivalence import VariantEquivalenceMerger
from redibis.pii.text_preprocess.registry import (
    TextExpander,
    resolve_expanders,
)
from redibis.pii.text_preprocess.surface import SurfaceSpan
from redibis.pii.text_preprocess.validators import route_canonical

logger = logging.getLogger("pii.text_preprocess")

_KIND_ROUTE = {
    "arabic_spoken_digits": "digits",
    "parenthesized_digits": "digits",
    "digit_cluster": "digits",
    "spaced_email": "email",
    "age_phrase": "age",
    "labeled_secret": "secret",
}


class ObfuscationPipeline:
    """Deterministic free-text preprocessing recognizer.

    Emits ``Candidate`` spans whose ``start``/``end``/``text`` always refer to
    the original Unicode surface. Canonical forms are stored in
    ``recognizer`` as ``"{variant_kind}|{canonical}"`` for equivalence merging.
    """

    name = "preprocess"

    def __init__(
        self,
        expanders: Sequence[TextExpander] | None = None,
        *,
        expander_names: Sequence[str] | None = None,
    ):
        if expanders is not None:
            self._expanders = list(expanders)
        else:
            self._expanders = resolve_expanders(expander_names)
        self._merger = VariantEquivalenceMerger()

    def recognize(self, text: str, ctx: RecognizeContext) -> list[Candidate]:
        if not text:
            return []
        spans: list[SurfaceSpan] = []
        for expander in self._expanders:
            try:
                spans.extend(expander.expand(text, ctx))
            except Exception as exc:
                logger.warning("expander %s failed: %s", getattr(expander, "name", "?"), exc)

        candidates: list[Candidate] = []
        seen: set[tuple[int, int, str, str]] = set()
        for span in spans:
            if span.start < 0 or span.end > len(text) or span.start >= span.end:
                continue
            surface = text[span.start:span.end]
            if surface != span.surface and span.surface:
                # Trust source offsets
                surface = surface
            key = (span.start, span.end, span.canonical, span.variant_kind)
            if key in seen:
                continue
            seen.add(key)

            route_kind = _KIND_ROUTE.get(span.variant_kind, "digits")
            outcome = route_canonical(
                span.canonical,
                ctx=ctx,
                entity_hint=span.entity_hint,
                label=span.label,
                kind=route_kind,
            )
            if not outcome.ok:
                continue
            if ctx.entities:
                allowed = set(ctx.entities)
                if outcome.entity_type not in allowed:
                    continue
            candidates.append(Candidate(
                entity_type=outcome.entity_type,
                score=outcome.score,
                engine="preprocess",
                start=span.start,
                end=span.end,
                text=surface,
                recognizer=f"{span.variant_kind}|{span.canonical}",
                validator=outcome.validator,
                context_boost=span.context_boost,
                is_proposal=outcome.is_proposal,
            ))

        return self._merger.merge(candidates, text=text)


def default_pipeline(*, expander_names: Sequence[str] | None = None) -> ObfuscationPipeline:
    return ObfuscationPipeline(expander_names=expander_names)
