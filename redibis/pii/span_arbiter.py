"""Reconcile LLM span proposals against deterministic engine candidates.

The span-level analogue of ``redibis.pii.equations.decide_pii``. Same mode
names, same Thresholds object, same decision channel — an operator learns one
reconciliation model, not two.

INVARIANT: a candidate carrying a ``validator`` is never re-typed, never
vetoed, and never shrunk by an LLM. See the module tests.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable, Optional, Sequence

from redibis.pii.scan.result import Candidate
from redibis.pii.thresholds import DEFAULT_EQUATION, EQUATION_MODES, Thresholds

# A validated span can never be un-flagged or re-typed by a language model.
# Candidate.validator is non-empty when a deterministic check passed: Luhn on
# a card, libphonenumber parse on an MSISDN, the Egyptian NID checksum. Those
# are arithmetic facts about the string. A model's opinion is evidence about
# meaning. Evidence does not overturn arithmetic.

AGREEMENT = (
    "confirmed",
    "unconfirmed",
    "type_conflict",
    "boundary_conflict",
    "llm_only",
    "vetoed",
)

_DET_ENGINES = frozenset({"regex", "phone", "preprocess", "ner"})
_CONFIRMED_BUMP_CAP = 0.08


@dataclass(frozen=True)
class LlmReview:
    """Parsed LLM verdict on an existing engine span. ``reason`` is scrubbed."""

    start: int
    end: int
    verdict: str  # "PII" | "NOT_PII" | "UNSURE"
    entity_type: str = ""
    confidence: float = 0.0
    reason: str = ""


@dataclass(frozen=True)
class ArbitrationRecord:
    span: tuple[int, int]
    entity_type: str            # after arbitration
    agreement: str              # one of AGREEMENT
    winner_engine: str
    mode: str
    rule: str                   # the branch that fired, e.g. "llm_type_wins_unvalidated"
    engine_type: str = ""       # what the deterministic engine said
    llm_type: str = ""          # what the LLM said
    llm_score: float | None = None
    llm_reason: str = ""        # model's own words, clipped and scrubbed
    validated: bool = False
    competing: tuple[dict, ...] = ()   # every candidate considered, winner included
    llm_verdict: str = ""
    final_score: float = 0.0
    candidates: tuple[Candidate, ...] = ()  # winner first; not serialized


def _brief(c: Candidate) -> dict:
    return {
        "entity_type": c.entity_type,
        "engine": c.engine,
        "score": round(float(c.score), 4),
        "start": c.start,
        "end": c.end,
        "validator": c.validator or "",
        "is_proposal": bool(c.is_proposal),
        "recognizer": c.recognizer or "",
    }


def _is_det(c: Candidate) -> bool:
    return (c.engine or "") in _DET_ENGINES


def _is_llm(c: Candidate) -> bool:
    return (c.engine or "") == "llm"


def _overlaps(a: Candidate, b: Candidate) -> bool:
    if a.start is None or a.end is None or b.start is None or b.end is None:
        return False
    return a.start < b.end and b.start < a.end


def _span_overlaps(start: int, end: int, c: Candidate) -> bool:
    if c.start is None or c.end is None:
        return False
    return start < c.end and c.start < end


def _authority(c: Candidate) -> tuple:
    from redibis.pii.rules.resolver import _authority as resolver_authority
    return resolver_authority(c)


def _best(cands: Sequence[Candidate]) -> Candidate:
    return max(cands, key=_authority)


def _with_text(c: Candidate, text: str, start: int, end: int) -> Candidate:
    start = max(0, min(start, len(text)))
    end = max(start, min(end, len(text)))
    return replace(c, start=start, end=end, text=text[start:end])


def _record(
    *,
    winner: Candidate,
    agreement: str,
    mode: str,
    rule: str,
    engine: Candidate | None = None,
    llm: Candidate | None = None,
    review: LlmReview | None = None,
    competing: Sequence[Candidate] = (),
    llm_verdict: str = "",
) -> ArbitrationRecord:
    span = (int(winner.start or 0), int(winner.end or 0))
    llm_score = None
    llm_type = ""
    llm_reason = ""
    if llm is not None:
        llm_score = float(llm.score)
        llm_type = llm.entity_type
    if review is not None:
        llm_score = float(review.confidence) if llm_score is None else llm_score
        llm_type = llm_type or (review.entity_type or "")
        llm_reason = review.reason or ""
        llm_verdict = llm_verdict or review.verdict
    everyone = tuple(competing) if competing else tuple(
        x for x in (winner, engine, llm) if x is not None
    )
    if everyone and everyone[0] is not winner:
        everyone = (winner,) + tuple(c for c in everyone if c is not winner)
    return ArbitrationRecord(
        span=span,
        entity_type=winner.entity_type,
        agreement=agreement,
        winner_engine=winner.engine,
        mode=mode,
        rule=rule,
        engine_type=engine.entity_type if engine is not None else "",
        llm_type=llm_type,
        llm_score=llm_score,
        llm_reason=llm_reason,
        validated=bool(engine.validator) if engine is not None else bool(winner.validator),
        competing=tuple(_brief(c) for c in everyone),
        llm_verdict=llm_verdict,
        final_score=float(winner.score),
        candidates=everyone,
    )


def _emit_decision(record: ArbitrationRecord) -> None:
    from redibis.obs import DecisionRecord, decision

    decision(
        DecisionRecord(
            stage="pii.text.arbitrate",
            fn="arbitrate",
            table="",
            column="",
            verdict=record.agreement,
            confidence=float(record.final_score),
            rule=record.rule,
            inputs={
                "mode": record.mode,
                "engine": record.winner_engine,
                "engine_type": record.engine_type,
                "llm_type": record.llm_type,
                "llm_score": record.llm_score,
                "validated": record.validated,
                "span": list(record.span),
            },
        )
    )


def _clusters(cands: Sequence[Candidate]) -> list[list[Candidate]]:
    remaining = [c for c in cands if c.start is not None and c.end is not None]
    groups: list[list[Candidate]] = []
    while remaining:
        seed = remaining.pop(0)
        group = [seed]
        changed = True
        while changed:
            changed = False
            keep: list[Candidate] = []
            for other in remaining:
                if any(_overlaps(other, g) for g in group):
                    group.append(other)
                    changed = True
                else:
                    keep.append(other)
            remaining = keep
        groups.append(group)
    return groups


def _widen(
    engine: Candidate,
    llm: Candidate,
    text: str,
    *,
    allow_shrink: bool,
) -> Candidate:
    from redibis.pii.rules.text_filters import BoundaryNormalizer, enclosing_sentence

    assert engine.start is not None and engine.end is not None
    assert llm.start is not None and llm.end is not None
    sent_s, sent_e = enclosing_sentence(text, engine.start, engine.end)
    union_s = min(engine.start, llm.start)
    union_e = max(engine.end, llm.end)
    new_s = max(sent_s, union_s)
    new_e = min(sent_e, union_e)
    if engine.validator or not allow_shrink:
        new_s = min(new_s, engine.start)
        new_e = max(new_e, engine.end)
    if new_e <= new_s:
        return engine
    widened = _with_text(engine, text, new_s, new_e)
    normalized = BoundaryNormalizer().apply([widened], text)
    if not normalized:
        return engine
    out = normalized[0]
    if engine.validator:
        # Never shrink below the validated core, even after punctuation trim.
        if (out.start or 0) > engine.start or (out.end or 0) < engine.end:
            out = _with_text(engine, text, min(out.start or engine.start, engine.start),
                             max(out.end or engine.end, engine.end))
    return out


def _bump_confirmed(engine: Candidate, llm: Candidate) -> Candidate:
    extra = min(_CONFIRMED_BUMP_CAP, max(0.0, (llm.score - engine.score) * 0.5))
    if extra <= 0:
        extra = min(_CONFIRMED_BUMP_CAP, 0.03)
    return replace(engine, score=min(1.0, engine.score + extra))


def _promote(c: Candidate) -> Candidate:
    return replace(c, is_proposal=False) if c.is_proposal else c


def _apply_vetoes(
    det: list[Candidate],
    reviews: Sequence[LlmReview],
    *,
    mode: str,
    thresholds: Thresholds,
) -> tuple[list[Candidate], list[ArbitrationRecord], set[int]]:
    """Return (kept det, veto records, indices of vetoed det candidates)."""
    if mode == "independent" or not reviews:
        return list(det), [], set()
    dropped: set[int] = set()
    records: list[ArbitrationRecord] = []
    llm_min = float(thresholds.llm_min)
    presidio_min = float(thresholds.presidio_min)
    for review in reviews:
        if review.verdict != "NOT_PII":
            continue
        for i, cand in enumerate(det):
            if i in dropped:
                continue
            if not _span_overlaps(review.start, review.end, cand):
                continue
            validated = bool(cand.validator)
            if validated:
                records.append(_record(
                    winner=cand, agreement="unconfirmed", mode=mode,
                    rule="llm_veto_blocked_validated", engine=cand, review=review,
                    llm_verdict="NOT_PII", competing=(cand,),
                ))
                continue
            if mode == "strict":
                if review.confidence < llm_min:
                    records.append(_record(
                        winner=cand, agreement="unconfirmed", mode=mode,
                        rule="llm_veto_below_llm_min", engine=cand, review=review,
                        llm_verdict="NOT_PII", competing=(cand,),
                    ))
                    continue
                if cand.score >= presidio_min:
                    records.append(_record(
                        winner=cand, agreement="unconfirmed", mode=mode,
                        rule="llm_veto_engine_above_presidio_min", engine=cand,
                        review=review, llm_verdict="NOT_PII", competing=(cand,),
                    ))
                    continue
            elif mode == "balanced" and review.confidence < llm_min:
                records.append(_record(
                    winner=cand, agreement="unconfirmed", mode=mode,
                    rule="llm_veto_below_llm_min", engine=cand, review=review,
                    llm_verdict="NOT_PII", competing=(cand,),
                ))
                continue
            dropped.add(i)
            records.append(_record(
                winner=cand, agreement="vetoed", mode=mode,
                rule="llm_veto_unvalidated", engine=cand, review=review,
                llm_verdict="NOT_PII", competing=(cand,),
            ))
    kept = [c for i, c in enumerate(det) if i not in dropped]
    return kept, records, dropped


def _llm_only_keep(
    llm: Candidate,
    *,
    mode: str,
    thresholds: Thresholds,
) -> tuple[Optional[Candidate], str, str]:
    """Return (candidate-or-None, agreement, rule)."""
    llm_min = float(thresholds.llm_min)
    if mode == "independent":
        return llm, "llm_only", "llm_only_proposal"
    if mode == "strict":
        if llm.score >= llm_min:
            return llm, "llm_only", "llm_only_proposal"
        return None, "llm_only", "llm_only_dropped_below_llm_min"
    if mode == "balanced":
        if llm.score >= llm_min:
            return _promote(llm), "llm_only", "llm_only_promoted"
        return llm, "llm_only", "llm_only_proposal"
    # lenient
    return _promote(llm), "llm_only", "llm_only_promoted"


def _type_conflict_llm_wins(mode: str, engine: Candidate, llm: Candidate, thresholds: Thresholds) -> tuple[bool, str]:
    if engine.validator:
        return False, "engine_type_wins_validated"
    if mode == "strict":
        return False, "engine_type_wins_strict"
    if mode == "independent":
        return False, "engine_type_wins_independent"
    if mode == "balanced":
        if llm.score >= float(thresholds.llm_min):
            return True, "llm_type_wins_unvalidated"
        return False, "engine_type_wins_below_llm_min"
    # lenient
    return True, "llm_type_wins_unvalidated"


def _boundary_widen(mode: str) -> bool:
    return mode in ("balanced", "lenient")


def _arbitration_for_cluster(
    group: Sequence[Candidate],
    *,
    text: str,
    mode: str,
    thresholds: Thresholds,
    llm_ran: bool,
    reviews: Sequence[LlmReview],
) -> tuple[list[Candidate], list[ArbitrationRecord]]:
    det = [c for c in group if _is_det(c)]
    llm_c = [c for c in group if _is_llm(c)]
    other = [c for c in group if not _is_det(c) and not _is_llm(c)]
    out: list[Candidate] = list(other)
    records: list[ArbitrationRecord] = []

    overlapping_reviews = [
        r for r in reviews
        if any(_span_overlaps(r.start, r.end, c) for c in det)
    ]
    review = overlapping_reviews[0] if overlapping_reviews else None
    llm_verdict = ""
    if review is not None:
        llm_verdict = review.verdict
    elif llm_c:
        llm_verdict = "PII"

    if det and not llm_c:
        for cand in det:
            out.append(cand)
            if llm_ran:
                records.append(_record(
                    winner=cand, agreement="unconfirmed", mode=mode,
                    rule="engine_only_unconfirmed", engine=cand, review=review,
                    llm_verdict=llm_verdict or "UNSURE", competing=(cand,),
                ))
        return out, records

    if llm_c and not det:
        for cand in llm_c:
            kept, agreement, rule = _llm_only_keep(cand, mode=mode, thresholds=thresholds)
            if kept is not None:
                out.append(kept)
                records.append(_record(
                    winner=kept, agreement=agreement, mode=mode, rule=rule,
                    llm=cand, llm_verdict="PII", competing=(kept,),
                ))
            else:
                records.append(_record(
                    winner=cand, agreement=agreement, mode=mode, rule=rule,
                    llm=cand, llm_verdict="PII", competing=(cand,),
                ))
        return out, records

    engine = _best(det)
    llm = _best(llm_c)
    same_type = engine.entity_type == llm.entity_type
    same_span = engine.start == llm.start and engine.end == llm.end

    if same_type and same_span:
        bumped = _bump_confirmed(engine, llm)
        out.append(bumped)
        # Keep the LLM candidate so SpanResolver can attach it as a loser.
        if mode == "independent":
            out.extend(c for c in group if c is not engine)
        records.append(_record(
            winner=bumped, agreement="confirmed", mode=mode,
            rule="agreement_confirmed", engine=engine, llm=llm,
            llm_verdict=llm_verdict or "PII", competing=(bumped, llm),
        ))
        return out, records

    if not same_type:
        llm_wins, rule = _type_conflict_llm_wins(mode, engine, llm, thresholds)
        if llm_wins:
            winner = replace(llm, is_proposal=False)
            if _boundary_widen(mode) and not (engine.start == llm.start and engine.end == llm.end):
                if not engine.validator:
                    winner = replace(
                        _widen(engine, llm, text, allow_shrink=True),
                        entity_type=llm.entity_type,
                        engine="llm",
                        is_proposal=False,
                        score=llm.score,
                        recognizer=llm.recognizer,
                    )
                else:
                    # Type is frozen on validated spans — this branch is unreachable
                    # because llm_wins is False when validated. Kept as a belt.
                    winner = engine
                    rule = "engine_type_wins_validated"
            out.append(winner)
            records.append(_record(
                winner=winner, agreement="type_conflict", mode=mode, rule=rule,
                engine=engine, llm=llm, llm_verdict=llm_verdict or "PII",
                competing=(winner, engine, llm),
            ))
        else:
            out.append(engine)
            if mode == "independent":
                out.extend(c for c in group if c is not engine)
            records.append(_record(
                winner=engine, agreement="type_conflict", mode=mode, rule=rule,
                engine=engine, llm=llm, llm_verdict=llm_verdict or "PII",
                competing=(engine, llm),
            ))
        return out, records

    # same type, different extent → boundary conflict
    if mode in ("independent", "strict") or not _boundary_widen(mode):
        out.append(engine)
        if mode == "independent":
            out.extend(c for c in group if c is not engine)
        records.append(_record(
            winner=engine, agreement="boundary_conflict", mode=mode,
            rule="engine_boundary_wins_strict" if mode == "strict" else "engine_boundary_wins_independent",
            engine=engine, llm=llm, llm_verdict=llm_verdict or "PII",
            competing=(engine, llm),
        ))
        return out, records

    widened = _widen(engine, llm, text, allow_shrink=not bool(engine.validator))
    out.append(widened)
    records.append(_record(
        winner=widened, agreement="boundary_conflict", mode=mode,
        rule="boundary_widen_sentence", engine=engine, llm=llm,
        llm_verdict=llm_verdict or "PII", competing=(widened, engine, llm),
    ))
    return out, records


def arbitrate(
    candidates: Sequence[Candidate],
    *,
    text: str,
    mode: str = DEFAULT_EQUATION,
    thresholds: Thresholds | None = None,
    llm_ran: bool = False,
    reviews: Sequence[LlmReview] | Iterable[LlmReview] = (),
) -> tuple[list[Candidate], list[ArbitrationRecord]]:
    """Return (candidates for SpanResolver, one record per contested region).

    ``independent`` (the default) is exactly today's behaviour: the LLM adds
    non-overlapping spans and never arbitrates. Candidates are returned
    unchanged so existing scans stay byte-identical.
    """
    if mode not in EQUATION_MODES:
        raise ValueError(
            f"Unknown equation mode {mode!r}. Valid modes: {sorted(EQUATION_MODES)}"
        )
    thr = thresholds or Thresholds()
    review_list = tuple(reviews or ())

    if mode == "independent":
        return list(candidates), []

    det = [c for c in candidates if _is_det(c)]
    llm_c = [c for c in candidates if _is_llm(c)]
    other = [c for c in candidates if not _is_det(c) and not _is_llm(c)]

    kept_det, veto_records, _dropped = _apply_vetoes(
        det, review_list, mode=mode, thresholds=thr,
    )
    for rec in veto_records:
        if rec.agreement in ("vetoed", "type_conflict", "boundary_conflict", "confirmed"):
            _emit_decision(rec)

    pooled = list(other) + kept_det + llm_c
    out: list[Candidate] = []
    records: list[ArbitrationRecord] = list(veto_records)
    for group in _clusters(pooled):
        kept, recs = _arbitration_for_cluster(
            group, text=text, mode=mode, thresholds=thr,
            llm_ran=llm_ran, reviews=review_list,
        )
        out.extend(kept)
        for rec in recs:
            records.append(rec)
            if rec.agreement in ("vetoed", "type_conflict", "boundary_conflict", "confirmed"):
                _emit_decision(rec)
    return out, records


def summarize_arbitration(
    records: Sequence[ArbitrationRecord],
    *,
    mode: str,
    include_records: bool = False,
) -> dict:
    counts = {
        "mode": mode,
        "contested": 0,
        "confirmed": 0,
        "unconfirmed": 0,
        "vetoed": 0,
        "type_conflicts": 0,
        "boundary_conflicts": 0,
        "llm_only": 0,
    }
    for rec in records:
        if rec.agreement == "confirmed":
            counts["confirmed"] += 1
        elif rec.agreement == "unconfirmed":
            counts["unconfirmed"] += 1
        elif rec.agreement == "vetoed":
            counts["vetoed"] += 1
            counts["contested"] += 1
        elif rec.agreement == "type_conflict":
            counts["type_conflicts"] += 1
            counts["contested"] += 1
        elif rec.agreement == "boundary_conflict":
            counts["boundary_conflicts"] += 1
            counts["contested"] += 1
        elif rec.agreement == "llm_only":
            counts["llm_only"] += 1
    if include_records:
        counts["records"] = [record_to_dict(r) for r in records]
    return counts


def record_to_dict(rec: ArbitrationRecord) -> dict:
    d = {
        "span": list(rec.span),
        "entity_type": rec.entity_type,
        "agreement": rec.agreement,
        "winner_engine": rec.winner_engine,
        "mode": rec.mode,
        "rule": rec.rule,
        "validated": rec.validated,
        "competing": [dict(x) for x in rec.competing],
    }
    if rec.engine_type:
        d["engine_type"] = rec.engine_type
    if rec.llm_type:
        d["llm_type"] = rec.llm_type
    if rec.llm_score is not None:
        d["llm_score"] = rec.llm_score
    if rec.llm_reason:
        d["llm_reason"] = rec.llm_reason
    if rec.llm_verdict:
        d["llm_verdict"] = rec.llm_verdict
    return d


def stamp_detections(
    detections: Sequence,
    records: Sequence[ArbitrationRecord],
):
    """Attach agreement / llm_* fields and competing evidence onto detections."""
    from dataclasses import replace as dc_replace

    if not records:
        return list(detections)
    stamped = []
    used: set[int] = set()
    for d in detections:
        match = None
        match_i = None
        for i, rec in enumerate(records):
            if i in used:
                continue
            if d.start is None or d.end is None:
                continue
            if rec.span[0] < d.end and d.start < rec.span[1] and (
                rec.entity_type == d.entity_type or rec.winner_engine == d.engine
            ):
                match = rec
                match_i = i
                break
        if match is None:
            stamped.append(d)
            continue
        used.add(match_i)  # type: ignore[arg-type]
        extra = match.candidates
        evidence = d.evidence or ()
        if extra:
            seen = {(c.engine, c.start, c.end, c.entity_type) for c in evidence}
            merged = list(evidence)
            for c in extra:
                key = (c.engine, c.start, c.end, c.entity_type)
                if key not in seen:
                    merged.append(c)
                    seen.add(key)
            evidence = tuple(merged)
        kwargs = {
            "agreement": match.agreement,
            "arbitration_rule": match.rule,
            "evidence": evidence,
        }
        if match.llm_verdict:
            kwargs["llm_verdict"] = match.llm_verdict
        if match.llm_score is not None:
            kwargs["llm_score"] = match.llm_score
        if match.llm_reason:
            kwargs["llm_reason"] = match.llm_reason
        try:
            stamped.append(dc_replace(d, **kwargs))
        except TypeError:
            stamped.append(d)
    return stamped
