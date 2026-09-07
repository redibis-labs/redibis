from __future__ import annotations

from dataclasses import dataclass

#: Default equation mode used when none is specified.
DEFAULT_EQUATION: str = "independent"

#: Valid equation mode names.
EQUATION_MODES: frozenset[str] = frozenset({"strict", "balanced", "lenient", "independent"})


@dataclass
class Thresholds:
    """
    Per-engine confidence floors and equation tuning knobs.

    These are independent of the equation *mode* — they control the confidence
    *bar* for each engine.  Changing a threshold shifts which detections are
    considered a "yes" vote; it does NOT change the voting logic itself.

    Attributes
    ----------
    presidio_min
        Minimum Presidio score to count as a positive vote.
    gliner_min
        Minimum GLiNER score to count as a positive vote.
    llm_min
        Minimum LLM score to count as a positive vote.
    phone_min
        Minimum libphonenumber / MSISDN-plan score to count as a positive vote.
    nid_min
        Minimum Egyptian National ID column valid-rate to count as a positive
        vote when the entity is ``EG_NATIONAL_ID`` (stricter than phone — NIDs
        are fixed-width).
    learned_min
        Minimum structured learned-classifier score to count as a positive vote.
        High default (0.90) — vote-safety until a model is steward-promoted.
    learned_enabled
        When False (default), ``learned_score`` never votes.
    learned_promoted
        When False (default), learned votes only in ``balanced`` (needs a second
        engine). When True, learned may vote in strict/lenient/independent too.
    ge_triage_min
        Minimum GE triage score to send a column to the NER detector.
    very_high_confidence_floor
        Score at or above which a single engine triggers detection in
        ``balanced`` mode (the "very high confidence" shortcut).
    """
    presidio_min:               float = 0.80
    gliner_min:                 float = 0.70
    llm_min:                    float = 0.82
    phone_min:                  float = 0.80
    nid_min:                    float = 0.85
    imei_min:                   float = 0.85
    imsi_min:                   float = 0.90
    geo_min:                    float = 0.75
    learned_min:                float = 0.90
    learned_enabled:            bool = False
    learned_promoted:           bool = False
    ge_triage_min:              float = 0.0
    very_high_confidence_floor: float = 0.90
