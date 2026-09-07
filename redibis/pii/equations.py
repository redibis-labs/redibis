"""
pii_detection.decisions.equations
===================================
Pure decision functions — no I/O, no global state.

Each equation mode is a separate private function registered in the dispatch
table of ``decide_pii``.  Adding a new mode means adding one private function
and one entry in the dispatch dict — nothing else changes.

Equation modes
--------------
strict    All available engines must vote yes at or above their threshold.
balanced  Any 2-of-3 engines vote yes, OR one engine with very-high confidence
          (score >= Thresholds.very_high_confidence_floor).
lenient   Any single engine above its threshold triggers detection.
independent Evaluates each engine independently. A single engine above its specific 
          threshold triggers a detection (avoids low scores from dragging others down).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Callable, Optional

from redibis.models import PIIDetection, PIIColumnReport
from redibis.pii.thresholds import Thresholds, DEFAULT_EQUATION, EQUATION_MODES

_EquationFn = Callable[[PIIDetection, Thresholds], PIIDetection]


def decide_pii(
    detection:  PIIDetection,
    equation:   str,
    thresholds: Thresholds,
) -> PIIDetection:
    """
    Apply an equation mode to a raw detection and return the verdict.

    Parameters
    ----------
    detection:
        A ``PIIDetection`` with raw engine scores and ``detected=False``.
    equation:
        One of ``"strict"``, ``"balanced"``, or ``"lenient"``.
    thresholds:
        Per-engine confidence floors and tuning knobs.

    Returns
    -------
    A new ``PIIDetection`` with ``detected``, ``confidence``, and
    ``equation_used`` populated.

    Plugin-only scores on ``detection.engine_evidence`` are **not** votes.
    Built-in modes only consider first-party fields (Presidio, GLiNER, phone,
    LLM refiner, learned, NID). Plugin records stay on the detection for
    investigation and replay of evidence, not for equation participation.

    Raises
    ------
    ValueError
        If *equation* is not a recognised mode.
    """
    _dispatch: dict[str, _EquationFn] = {
        "strict":      _decide_strict,
        "balanced":    _decide_balanced,
        "lenient":     _decide_lenient,
        "independent": _decide_independent,
    }
    if equation not in _dispatch:
        raise ValueError(
            f"Unknown equation mode {equation!r}. "
            f"Valid modes: {sorted(EQUATION_MODES)}"
        )
    result = _dispatch[equation](detection, thresholds)
    _emit_pii_decision(result, equation, thresholds)
    return result


def _emit_pii_decision(d: PIIDetection, equation: str, t: Thresholds) -> None:
    from redibis.obs import DecisionRecord, decision
    from redibis.pii.sensitivity import classify_sensitivity

    # ``detected`` is the evidence-layer verdict (an engine found something
    # worth flagging); the entity's sensitivity classification -- computed
    # here purely for audit visibility, never fed back into the vote -- is
    # what later decides whether it is treated as personal data (e.g. an
    # ``ORGANIZATION`` match stays "internal" and carries no PII tags/masking
    # downstream even when ``detected=True``).
    classification = classify_sensitivity(d.entity_type) if d.detected else None

    decision(
        DecisionRecord(
            stage="pii",
            fn="pii.equations.decide_pii",
            table="",
            column=d.column,
            verdict="pii" if d.detected else "not_pii",
            confidence=d.confidence,
            rule=decision_rule_text(equation, t),
            inputs={
                "equation": equation,
                "presidio_score": d.presidio_score,
                "gliner_score": d.gliner_score,
                "llm_score": d.llm_score,
                "phone_score": d.phone_score,
                "learned_score": d.learned_score,
                "msisdn_valid_rate": d.msisdn_valid_rate,
                "nid_valid_rate": getattr(d, "nid_valid_rate", None),
                "presidio_min": t.presidio_min,
                "gliner_min": t.gliner_min,
                "llm_min": t.llm_min,
                "learned_min": getattr(t, "learned_min", None),
                "learned_enabled": getattr(t, "learned_enabled", False),
                "nid_min": getattr(t, "nid_min", 0.85),
                "pattern": d.presidio_pattern or "",
                "entity_type": d.entity_type or "",
                "match_rate": d.presidio_match_rate,
                "classification": classification,
            },
        )
    )


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _phone_vote(d: PIIDetection, t: Thresholds) -> bool:
    from redibis.pii.telecom_signals import phone_verdict_eligible, presidio_phone_signal

    pres_entity, pres_score = presidio_phone_signal(d)
    phone_state = (getattr(d, "engine_states", {}) or {}).get("phone", {})
    phonenumbers_ran = bool(phone_state.get("ran"))
    use_phonenumbers = phone_state.get("enabled", True)
    if use_phonenumbers is False:
        use_phonenumbers = d.phone_valid_rate is not None or phonenumbers_ran
    return phone_verdict_eligible(
        column_name=d.column,
        phone_score=d.phone_score,
        phone_min=t.phone_min,
        presidio_entity=pres_entity or d.entity_type,
        presidio_score=pres_score if pres_score is not None else d.presidio_score,
        ner_label=d.gliner_label,
        ner_score=d.gliner_score,
        presidio_min=t.presidio_min,
        ner_min=t.gliner_min,
        use_phonenumbers=bool(use_phonenumbers),
        phone_valid_rate=d.phone_valid_rate,
        phonenumbers_ran=phonenumbers_ran,
    )


def _presidio_vote(d: PIIDetection, t: Thresholds) -> bool:
    """Regex vote — PHONE_NUMBER requires the same context gate as the phone engine."""
    from redibis.pii.telecom_signals import _is_phone_label, presidio_phone_signal

    if d.presidio_score is None or d.presidio_score < t.presidio_min:
        return False
    if not _structural_gates_for(d, t):
        return False
    pres_entity, pres_score = presidio_phone_signal(d)
    if pres_entity or _is_phone_label(d.entity_type):
        return _phone_vote(d, t)
    return True


def _ner_vote(d: PIIDetection, t: Thresholds) -> bool:
    from redibis.pii.telecom_signals import _is_phone_label

    if d.gliner_score is None or d.gliner_score < t.gliner_min:
        return False
    if not _structural_gates_for(d, t):
        return False
    if _is_phone_label(d.gliner_label):
        return _phone_vote(d, t)
    return True


def _nid_gate_passed(
    *,
    entity_is_nid: bool,
    nid_valid_rate: float | None,
    nid_min: float,
    nid_ran: bool = False,
) -> bool:
    """Column-level EG_NATIONAL_ID gate — mirrors ``_phonenumbers_gate_passed``."""
    if not entity_is_nid:
        return True
    if nid_valid_rate is not None:
        return nid_valid_rate >= nid_min
    if nid_ran:
        return False
    return True


def _is_nid_entity(entity_type: str | None) -> bool:
    if not entity_type:
        return False
    # Gate applies to Egyptian NID only — not locale packs that reuse NATIONAL_ID
    # (e.g. French NIR).
    return entity_type.upper().replace(" ", "_") == "EG_NATIONAL_ID"


def _nid_gate_for(d: PIIDetection, t: Thresholds) -> bool:
    nid_state = (getattr(d, "engine_states", {}) or {}).get("nid", {})
    nid_ran = bool(nid_state.get("ran"))
    return _nid_gate_passed(
        entity_is_nid=_is_nid_entity(d.entity_type),
        nid_valid_rate=getattr(d, "nid_valid_rate", None),
        nid_min=float(getattr(t, "nid_min", 0.85)),
        nid_ran=nid_ran,
    )


def _rate_gate_passed(
    *,
    entity_matches: bool,
    valid_rate: float | None,
    floor: float,
    ran: bool = False,
) -> bool:
    """Generic column-rate gate — same three-branch shape as phone/NID."""
    if not entity_matches:
        return True
    if valid_rate is not None:
        return valid_rate >= floor
    if ran:
        return False
    return True


def _structural_gates_for(d: PIIDetection, t: Thresholds) -> bool:
    """Apply NID / IMEI / IMSI / LOCATION column-rate gates."""
    if not _nid_gate_for(d, t):
        return False
    et = (d.entity_type or "").upper().replace(" ", "_")
    states = getattr(d, "engine_states", {}) or {}
    if not _rate_gate_passed(
        entity_matches=(et == "IMEI"),
        valid_rate=getattr(d, "imei_valid_rate", None),
        floor=float(getattr(t, "imei_min", 0.85)),
        ran=bool((states.get("imei") or {}).get("ran")),
    ):
        return False
    if not _rate_gate_passed(
        entity_matches=(et == "IMSI"),
        valid_rate=getattr(d, "imsi_valid_rate", None),
        floor=float(getattr(t, "imsi_min", 0.90)),
        ran=bool((states.get("imsi") or {}).get("ran")),
    ):
        return False
    if not _rate_gate_passed(
        entity_matches=(et == "LOCATION"),
        valid_rate=getattr(d, "geo_confidence", None),
        floor=float(getattr(t, "geo_min", 0.75)),
        ran=bool((states.get("geo") or {}).get("ran")),
    ):
        return False
    return True


def _learned_vote(d: PIIDetection, t: Thresholds, *, equation: str) -> bool:
    """Learned classifier vote with Phase-2 vote-safety defaults.

    Disabled unless ``t.learned_enabled``. Until ``t.learned_promoted``, the
    vote counts only in ``balanced`` (needs a second engine) and never alone
    via the very-high shortcut.
    """
    if not getattr(t, "learned_enabled", False):
        return False
    if d.learned_score is None:
        return False
    if d.learned_score < float(getattr(t, "learned_min", 0.90)):
        return False
    if not getattr(t, "learned_promoted", False) and equation != "balanced":
        return False
    return True


def _assemble(d: PIIDetection, detected: bool, equation: str) -> PIIDetection:
    """Return a new PIIDetection with the verdict fields set."""
    confidence = 0.0
    if detected:
        scores = [
            s for s in (
                d.presidio_score, d.gliner_score, d.llm_score, d.phone_score, d.learned_score,
            )
            if s is not None
        ]
        confidence = max(scores) if scores else 0.0
    return replace(d, detected=detected, confidence=confidence, equation_used=equation)


def _decide_lenient(d: PIIDetection, t: Thresholds) -> PIIDetection:
    """Any single engine above its threshold triggers detection."""
    detected = (
        _presidio_vote(d, t)
        or _ner_vote(d, t)
        or (d.llm_score    is not None and d.llm_score    >= t.llm_min)
        or _phone_vote(d, t)
        or _learned_vote(d, t, equation="lenient")
    )
    if detected and not _structural_gates_for(d, t):
        detected = False
    return _assemble(d, detected, equation="lenient")


def _decide_independent(d: PIIDetection, t: Thresholds) -> PIIDetection:
    """Independent evaluation: any single engine reaching its threshold flags the column."""
    detected = (
        _presidio_vote(d, t)
        or _ner_vote(d, t)
        or (d.llm_score    is not None and d.llm_score    >= t.llm_min)
        or _phone_vote(d, t)
        or _learned_vote(d, t, equation="independent")
    )
    if detected and not _structural_gates_for(d, t):
        detected = False
    return _assemble(d, detected, equation="independent")


def _decide_strict(d: PIIDetection, t: Thresholds) -> PIIDetection:
    """All available engines must vote yes."""
    votes = []
    if d.presidio_score is not None:
        votes.append(_presidio_vote(d, t))
    if d.gliner_score is not None:
        votes.append(_ner_vote(d, t))
    if d.llm_score is not None:
        votes.append(d.llm_score >= t.llm_min)
    if d.phone_score is not None:
        votes.append(_phone_vote(d, t))
    # Learned participates in strict only when enabled AND promoted.
    if (
        d.learned_score is not None
        and getattr(t, "learned_enabled", False)
        and getattr(t, "learned_promoted", False)
    ):
        votes.append(d.learned_score >= float(getattr(t, "learned_min", 0.90)))
    detected = bool(votes) and all(votes)
    if detected and not _structural_gates_for(d, t):
        detected = False
    return _assemble(d, detected, equation="strict")


def _decide_balanced(d: PIIDetection, t: Thresholds) -> PIIDetection:
    """Any 2-of-N engines vote yes, OR one engine with very-high confidence."""
    votes = []
    if d.presidio_score is not None:
        votes.append(_presidio_vote(d, t))
    if d.gliner_score is not None:
        votes.append(_ner_vote(d, t))
    if d.llm_score is not None:
        votes.append(d.llm_score >= t.llm_min)
    if d.phone_score is not None:
        votes.append(_phone_vote(d, t))
    if d.learned_score is not None and _learned_vote(d, t, equation="balanced"):
        votes.append(True)

    yes_count = sum(votes)
    floor = t.very_high_confidence_floor
    # Learned never drives the very-high single-engine shortcut until promoted.
    very_high_scores = [d.presidio_score, d.gliner_score, d.llm_score, d.phone_score]
    if getattr(t, "learned_promoted", False):
        very_high_scores.append(d.learned_score)
    very_high = any(
        s is not None and s >= floor
        for s in very_high_scores
    )
    detected = (yes_count >= 2) or (yes_count >= 1 and very_high)
    if detected and not _structural_gates_for(d, t):
        detected = False
    return _assemble(d, detected, equation="balanced")


# ─────────────────────────────────────────────────────────────────────────────
# PIIColumnReport builder (formal per-engine + conclusion + rule — v2 §1.5)
# ─────────────────────────────────────────────────────────────────────────────

def decision_rule_text(equation: str, t: Thresholds) -> str:
    """Human-readable description of the rule a given equation mode applies."""
    rx = f"regex >= {t.presidio_min:g}"
    nr = f"ner >= {t.gliner_min:g}"
    lm = f"llm >= {t.llm_min:g}"
    ph = f"phone >= {t.phone_min:g}"
    ld = f"learned >= {getattr(t, 'learned_min', 0.90):g}"
    if equation == "strict":
        return f"ALL available engines pass ({rx} AND {nr} AND {lm} AND {ph} AND {ld})"
    if equation == "balanced":
        return (f"2-of-N engines pass ({rx}, {nr}, {lm}, {ph}, {ld}) "
                f"OR any engine >= {t.very_high_confidence_floor:g}")
    # lenient / independent share the any-engine rule
    return f"{rx} OR {nr} OR {lm} OR {ph} OR {ld}"


def _basis_engine(d: PIIDetection, t: Thresholds) -> str:
    """Which engine drove the positive verdict (highest passing score wins)."""
    candidates = []
    if d.presidio_score is not None and _presidio_vote(d, t):
        candidates.append(("regex", d.presidio_score))
    if d.gliner_score is not None and _ner_vote(d, t):
        candidates.append(("ner", d.gliner_score))
    if d.llm_score is not None and d.llm_score >= t.llm_min:
        candidates.append(("llm", d.llm_score))
    if d.phone_score is not None and _phone_vote(d, t):
        candidates.append(("phone", d.phone_score))
    if d.learned_score is not None and _learned_vote(d, t, equation=d.equation_used or "balanced"):
        candidates.append(("learned", d.learned_score))
    if not candidates:
        # No engine passed its floor — fall back to whichever scored highest.
        scored = [
            ("regex", d.presidio_score),
            ("ner", d.gliner_score),
            ("llm", d.llm_score),
            ("phone", d.phone_score),
            ("learned", d.learned_score),
        ]
        scored = [(n, s) for n, s in scored if s is not None]
        if not scored:
            return ""
        return max(scored, key=lambda x: x[1])[0]
    return max(candidates, key=lambda x: x[1])[0]


def _copy_engine_state(d: PIIDetection, name: str) -> dict:
    states = getattr(d, "engine_states", {}) or {}
    raw = states.get(name, {}) if isinstance(states, dict) else {}
    return dict(raw) if isinstance(raw, dict) else {}


def _best_regex_entity(d: PIIDetection) -> str | None:
    if d.regex_hits:
        return d.regex_hits[0].get("entity_type")
    return d.entity_type if d.presidio_score is not None else None


def _best_ner_label(d: PIIDetection) -> str | None:
    if d.gliner_label:
        return d.gliner_label
    if d.ner_hits:
        return d.ner_hits[0].get("label")
    return None


def _engine_state_map(d: PIIDetection, t: Thresholds) -> dict[str, dict]:
    regex = _copy_engine_state(d, "regex")
    regex_ran = bool(regex.get("ran")) or any(
        value is not None for value in (d.presidio_score, d.presidio_match_rate, d.presidio_pattern)
    ) or bool(d.regex_hits)
    regex.update({
        "enabled": regex.get("enabled", regex_ran),
        "ran": regex_ran,
        "status": regex.get("status") or ("matched" if d.presidio_score is not None else ("no_match" if regex_ran else "not_run")),
        "score": d.presidio_score,
        "threshold": t.presidio_min,
        "confident": d.presidio_score is not None and d.presidio_score >= t.presidio_min,
        "vote": _presidio_vote(d, t) if d.presidio_score is not None else False,
        "entity_type": _best_regex_entity(d),
        "pattern": d.presidio_pattern,
        "match_rate": d.presidio_match_rate,
        "hit_count": len(d.regex_hits or []),
    })

    ner = _copy_engine_state(d, "ner")
    ner_ran = bool(ner.get("ran")) or any(
        value is not None for value in (d.gliner_score, d.gliner_match_rate, d.gliner_label)
    ) or bool(d.ner_hits)
    ner.update({
        "enabled": ner.get("enabled", ner_ran),
        "available": ner.get("available", ner_ran),
        "ran": ner_ran,
        "status": ner.get("status") or ("matched" if d.gliner_score is not None else ("no_match" if ner_ran else "not_run")),
        "score": d.gliner_score,
        "threshold": t.gliner_min,
        "confident": d.gliner_score is not None and d.gliner_score >= t.gliner_min,
        "vote": _ner_vote(d, t) if d.gliner_score is not None else False,
        "label": _best_ner_label(d),
        "match_rate": d.gliner_match_rate,
        "engine": d.ner_engine,
        "hit_count": len(d.ner_hits or []),
    })

    llm_ran = any(value is not None for value in (d.llm_score, d.llm_verdict, d.llm_reasoning))
    llm = {
        "enabled": llm_ran,
        "ran": llm_ran,
        "status": (
            str(d.llm_verdict).lower()
            if d.llm_verdict
            else ("matched" if d.llm_score is not None else "not_run")
        ),
        "score": d.llm_score,
        "threshold": t.llm_min,
        "confident": d.llm_score is not None and d.llm_score >= t.llm_min,
        "vote": d.llm_score is not None and d.llm_score >= t.llm_min,
        "verdict": d.llm_verdict,
        "reasoning": d.llm_reasoning,
    }

    phone = _copy_engine_state(d, "phone")
    phone_ran = bool(phone.get("ran")) or any(
        value is not None for value in (
            d.phone_score, d.phone_valid_rate, d.phone_mobile_rate, d.phone_entity, d.phone_regions,
        )
    )
    phone.update({
        "enabled": phone.get("enabled", phone_ran),
        "ran": phone_ran,
        "status": phone.get("status") or ("matched" if d.phone_score is not None else ("no_match" if phone_ran else "not_run")),
        "score": d.phone_score,
        "threshold": t.phone_min,
        "confident": d.phone_score is not None and d.phone_score >= t.phone_min,
        "vote": _phone_vote(d, t) if d.phone_score is not None else False,
        "entity_type": d.phone_entity,
        "msisdn_valid_rate": d.msisdn_valid_rate,
        "valid_rate": d.phone_valid_rate,
        "mobile_rate": d.phone_mobile_rate,
        "regions": d.phone_regions or {},
    })

    learned_ran = d.learned_score is not None
    learned = _copy_engine_state(d, "learned")
    learned.update({
        "enabled": learned.get("enabled", getattr(t, "learned_enabled", False)),
        "ran": learned_ran,
        "status": learned.get("status") or (
            "matched" if d.learned_score is not None else "not_run"
        ),
        "score": d.learned_score,
        "threshold": getattr(t, "learned_min", 0.90),
        "confident": (
            d.learned_score is not None
            and d.learned_score >= float(getattr(t, "learned_min", 0.90))
        ),
        "vote": _learned_vote(d, t, equation=d.equation_used or "balanced"),
        "label": d.learned_label,
        "entity_type": d.learned_entity,
        "engine": d.learned_engine,
        "promoted": getattr(t, "learned_promoted", False),
    })

    return {"regex": regex, "ner": ner, "llm": llm, "phone": phone, "learned": learned}


def build_column_report(
    detection: PIIDetection,
    thresholds: Optional[Thresholds] = None,
    equation: Optional[str] = None,
) -> PIIColumnReport:
    """
    Build the formal PIIColumnReport for one column from a decided detection.

    The detection should already have been run through ``decide_pii`` so that
    ``detected``/``confidence`` reflect the verdict. ``thresholds``/``equation``
    default to standard floors / the detection's own equation.
    """
    t = thresholds or Thresholds()
    eq = equation or detection.equation_used or DEFAULT_EQUATION
    basis = _basis_engine(detection, t) if detection.detected else ""

    advisory = None
    if detection.entity_type:
        from redibis.models import canonical_entity
        from redibis.pii.sensitivity import classify_sensitivity
        can_ent = canonical_entity(detection.entity_type)
        if classify_sensitivity(can_ent) == "security_sensitive" or can_ent == "OTP":
            advisory = "transient/credential column — consider dropping from the analytical contract or restricting access."

    return PIIColumnReport(
        column=detection.column,
        regex=detection.presidio_score,
        ner=detection.gliner_score,
        llm=detection.llm_score,
        phone=detection.phone_score,
        entity_type=detection.entity_type,
        is_pii=detection.detected,
        confidence=detection.confidence,
        basis=basis,
        decision_rule=decision_rule_text(eq, t),
        equation=eq,
        arabic_aware=detection.arabic_aware,
        arabic_fraction=detection.arabic_fraction,
        engine_state=_engine_state_map(detection, t),
        advisory=advisory,
    )
