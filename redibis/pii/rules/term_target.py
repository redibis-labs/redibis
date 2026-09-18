"""Advise which rule field would actually drop a span for a given term.

Uses the real filters. Never re-implements matching. Never scans the engines.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping, Optional, Sequence

from redibis.pii.rules.recognizers import _NerStoplist
from redibis.pii.rules.text_filters import apply_text_rule_filters
from redibis.pii.scan.result import Candidate
from redibis.pii.text_preprocess.expanders._util import noise_set, token_fold, tokenize_with_spans
from redibis.pii.text_rules import TextRuleOverlay, default_text_rules, merge_text_rules

TARGETS = ("noise_terms", "exclude_terms", "ner_stoplist", "forbidden_span")


@dataclass(frozen=True)
class TargetAdvice:
    target: str
    term: str
    would_remove: tuple[tuple[int, int, str], ...]
    reason: str
    warnings: tuple[str, ...] = ()
    default: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "term": self.term,
            "would_remove": [list(row) for row in self.would_remove],
            "reason": self.reason,
            "warnings": list(self.warnings),
            "default": bool(self.default),
        }


def _as_overlay(raw: Any) -> TextRuleOverlay:
    if isinstance(raw, TextRuleOverlay):
        return raw
    if isinstance(raw, Mapping):
        return merge_text_rules(default_text_rules(), TextRuleOverlay.from_dict(raw))
    return default_text_rules()


def _as_candidates(spans: Iterable[Any], text: str) -> list[Candidate]:
    out: list[Candidate] = []
    for span in spans or ():
        if isinstance(span, Candidate):
            out.append(span)
            continue
        if isinstance(span, Mapping):
            start = int(span.get("start") or 0)
            end = int(span.get("end") or 0)
            surface = span.get("text")
            if surface is None and 0 <= start <= end <= len(text):
                surface = text[start:end]
            out.append(Candidate(
                entity_type=str(span.get("entity_type") or ""),
                score=float(span.get("score") or 0.9),
                engine=str(span.get("engine") or "ner"),
                start=start,
                end=end,
                text=str(surface or ""),
                recognizer=str(span.get("recognizer") or ""),
                validator=str(span.get("validator") or ""),
            ))
            continue
        start = int(getattr(span, "start", 0) or 0)
        end = int(getattr(span, "end", 0) or 0)
        surface = getattr(span, "text", None)
        if surface is None and 0 <= start <= end <= len(text):
            surface = text[start:end]
        out.append(Candidate(
            entity_type=str(getattr(span, "entity_type", "") or ""),
            score=float(getattr(span, "score", 0.9) or 0.9),
            engine=str(getattr(span, "engine", "") or "ner"),
            start=start,
            end=end,
            text=str(surface or ""),
            recognizer=str(getattr(span, "recognizer", "") or ""),
            validator=str(getattr(span, "validator", "") or ""),
        ))
    return out


def _keys(cands: Sequence[Candidate]) -> set[tuple[int, int, str]]:
    return {
        (int(c.start or 0), int(c.end or 0), str(c.entity_type or ""))
        for c in cands
        if c.start is not None and c.end is not None
    }


def _removed(before: Sequence[Candidate], after: Sequence[Candidate]) -> tuple[tuple[int, int, str], ...]:
    gone = _keys(before) - _keys(after)
    return tuple(sorted(gone))


def _apply_target(
    target: str,
    term: str,
    *,
    text: str,
    candidates: Sequence[Candidate],
    overlay: TextRuleOverlay,
) -> tuple[tuple[int, int, str], ...]:
    if target == "forbidden_span":
        folded = token_fold(term)
        hits: list[tuple[int, int, str]] = []
        for cand in candidates:
            surface = cand.text if cand.text is not None else (
                text[cand.start:cand.end] if cand.start is not None and cand.end is not None else ""
            )
            if token_fold(surface) == folded:
                hits.append((int(cand.start or 0), int(cand.end or 0), str(cand.entity_type or "")))
        return tuple(hits)

    if target == "ner_stoplist":
        extra = TextRuleOverlay.from_dict({"ner_stoplist": {"*": [term]}})
        merged = merge_text_rules(overlay, extra)
        stoplist = _NerStoplist(merged)
        kept: list[Candidate] = []
        for cand in candidates:
            if cand.engine != "ner" and (cand.engine or "") not in ("", "ner"):
                # Stoplist is NER-only; leave other engines alone.
                kept.append(cand)
                continue
            if cand.start is None or cand.end is None:
                kept.append(cand)
                continue
            result = stoplist.apply(text, int(cand.start), int(cand.end), str(cand.entity_type or ""))
            if result is None:
                continue
            ns, ne, surface = result
            if (ns, ne) != (cand.start, cand.end):
                kept.append(replace(cand, start=ns, end=ne, text=surface))
            else:
                kept.append(cand)
        return _removed(candidates, kept)

    extra_payload: dict[str, Any]
    if target == "noise_terms":
        extra_payload = {"noise_terms": [term]}
    elif target == "exclude_terms":
        extra_payload = {"exclude_terms": [term]}
    else:
        return ()
    merged = merge_text_rules(overlay, TextRuleOverlay.from_dict(extra_payload))
    after = apply_text_rule_filters(candidates, text, merged)
    before = apply_text_rule_filters(candidates, text, overlay)
    # Diff against the overlay-filtered list so we only credit the new term.
    return _removed(before, after)


def _token_count(term: str) -> int:
    return len(tokenize_with_spans(term or ""))


def _inside_longer_span(term: str, spans: Sequence[Candidate], text: str) -> Candidate | None:
    folded = token_fold(term)
    if not folded:
        return None
    for cand in spans:
        surface = cand.text if cand.text is not None else (
            text[cand.start:cand.end] if cand.start is not None and cand.end is not None else ""
        )
        if token_fold(surface) == folded:
            continue
        tokens = [token_fold(tok) for tok, _s, _e in tokenize_with_spans(surface)]
        if folded in tokens:
            return cand
    return None


def _equals_span(term: str, spans: Sequence[Candidate], text: str) -> Candidate | None:
    folded = token_fold(term)
    for cand in spans:
        surface = cand.text if cand.text is not None else (
            text[cand.start:cand.end] if cand.start is not None and cand.end is not None else ""
        )
        if token_fold(surface) == folded:
            return cand
    return None


def _is_filler(term: str, overlay: TextRuleOverlay) -> bool:
    return token_fold(term) in noise_set(overlay=overlay)


def _ner_only(span: Candidate | None) -> bool:
    if span is None:
        return False
    return (span.engine or "") == "ner"


def _pick_default(
    term: str,
    *,
    text: str,
    spans: Sequence[Candidate],
    overlay: TextRuleOverlay,
    viable: Sequence[TargetAdvice],
    requested: str,
) -> str:
    if requested and any(v.target == requested and v.would_remove for v in viable):
        return requested
    tokens = _token_count(term)
    equal = _equals_span(term, spans, text)
    inside = _inside_longer_span(term, spans, text)
    if equal is not None and _ner_only(equal):
        if any(v.target == "ner_stoplist" and v.would_remove for v in viable):
            return "ner_stoplist"
    if tokens <= 1:
        if equal is not None:
            return "exclude_terms"
        if inside is not None:
            if _is_filler(term, overlay):
                return "noise_terms"
            return "forbidden_span"
        return "exclude_terms"
    # multi-token
    if equal is not None:
        return "exclude_terms"
    return "forbidden_span"


def advise(
    term: str,
    *,
    text: str,
    spans: Sequence[Any],
    overlay: Any = None,
    requested: str = "",
) -> list[TargetAdvice]:
    """For a term (or a whole span surface), say which rule field would drop which spans."""
    value = str(term or "").strip()
    if not value:
        return []
    ov = _as_overlay(overlay)
    cands = _as_candidates(spans, text)
    tokens = _token_count(value)
    inside = _inside_longer_span(value, cands, text)
    equal = _equals_span(value, cands, text)

    rows: list[TargetAdvice] = []
    for target in TARGETS:
        if target == "noise_terms" and tokens > 1:
            rows.append(TargetAdvice(
                target=target,
                term=value,
                would_remove=(),
                reason=(
                    f"{value!r} is more than one token — noise_terms only matches single "
                    "tokens, so this field cannot drop the span."
                ),
            ))
            continue
        removed = _apply_target(target, value, text=text, candidates=cands, overlay=ov)
        # A content word inside a longer name can shrink the span via edge-noise
        # stripping. That is not "removing the finding" and must not be offered
        # as a global noise_terms fix — reject the span or exclude the surface.
        if (
            target == "noise_terms"
            and inside is not None
            and equal is None
            and not _is_filler(value, ov)
        ):
            removed = ()
        warnings: list[str] = []
        if len(removed) > 1:
            warnings.append(f"also drops {len(removed) - 1} other span(s) in this text")
        if target == "forbidden_span":
            reason = (
                f"Mark {value!r} as forbidden in this text only "
                f"({len(removed)} matching span(s))."
                if removed else
                f"{value!r} does not equal any span surface in this text."
            )
        elif not removed:
            if inside is not None and target in ("exclude_terms", "noise_terms", "ner_stoplist"):
                surface = inside.text or text[inside.start:inside.end]
                reason = (
                    f"{value!r} sits inside {surface!r} — no rule field removes a word "
                    "from inside a name; reject the span instead, or exclude the whole surface."
                )
            else:
                reason = f"{value!r} on {target} would not remove any span in this text."
        else:
            reason = f"{target} would drop {len(removed)} span(s) in this text."
        rows.append(TargetAdvice(
            target=target,
            term=value,
            would_remove=removed,
            reason=reason,
            warnings=tuple(warnings),
        ))

    default_target = _pick_default(
        value, text=text, spans=cands, overlay=ov, viable=rows, requested=requested,
    )
    out: list[TargetAdvice] = []
    for row in rows:
        is_default = row.target == default_target and bool(row.would_remove)
        # forbidden_span can be the default even when it would "remove" via exact surface.
        if row.target == default_target and row.target == "forbidden_span" and (row.would_remove or inside):
            is_default = True
        out.append(replace(row, default=is_default))
    return out


def summarize_advice(rows: Sequence[TargetAdvice]) -> dict[str, Any]:
    """Top-level envelope for ``POST /api/gateway/rules/advise``.

    ``effective`` is true when any target would actually drop a span.
    ``reason`` is the first per-target sentence when nothing would drop.
    """
    advice = list(rows or ())
    effective = any(bool(r.would_remove) for r in advice)
    reason = ""
    if not effective:
        reason = next((r.reason for r in advice if r.reason), "")
    return {
        "effective": effective,
        "reason": reason,
        "advice": [r.to_dict() for r in advice],
    }


def ineffective_terms(
    draft: Mapping[str, Any] | None,
    *,
    text: str,
    spans: Sequence[Any],
    overlay: Any = None,
) -> list[dict[str, str]]:
    """Every term in ``draft`` that removed nothing in this text."""
    data = dict(draft or {})
    ov = _as_overlay(overlay)
    cands = _as_candidates(spans, text)
    flagged: list[dict[str, str]] = []
    for field in ("noise_terms", "exclude_terms"):
        for term in data.get(field) or ():
            value = str(term or "").strip()
            if not value:
                continue
            removed = _apply_target(field, value, text=text, candidates=cands, overlay=ov)
            if not removed:
                flagged.append({"field": field, "term": value})
    stoplist = data.get("ner_stoplist") or {}
    if isinstance(stoplist, Mapping):
        for _et, terms in stoplist.items():
            for term in terms or ():
                value = str(term or "").strip()
                if not value:
                    continue
                removed = _apply_target(
                    "ner_stoplist", value, text=text, candidates=cands, overlay=ov,
                )
                if not removed:
                    flagged.append({"field": "ner_stoplist", "term": value})
    return flagged
