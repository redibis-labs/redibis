"""Unified detection result models for column and free-text PII scanning."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Mapping, Optional

DetectionKind = Literal["span", "column"]
OFFSET_UNIT = "unicode_codepoint"


@dataclass(frozen=True)
class Candidate:
    """Raw recognizer hit — evidence only, not a verdict."""

    entity_type: str
    score: float
    engine: str  # "regex" | "phone" | "ner" | "llm" | "preprocess"
    start: Optional[int] = None
    end: Optional[int] = None
    text: Optional[str] = None
    recognizer: str = ""
    validator: str = ""
    context_boost: bool = False
    is_proposal: bool = False
    canonical: str = ""

    def to_dict(self, *, return_text: bool = True) -> dict:
        d = {
            "entity_type": self.entity_type,
            "score": round(self.score, 4),
            "engine": self.engine,
            "start": self.start,
            "end": self.end,
            "recognizer": self.recognizer,
            "validator": self.validator,
            "context_boost": self.context_boost,
            "is_proposal": self.is_proposal,
        }
        if self.canonical:
            d["canonical"] = self.canonical
        if return_text:
            d["text"] = self.text
        else:
            d["text"] = ""
        return d


@dataclass(frozen=True)
class Detection:
    """Resolved finding — a text span or a column verdict."""

    entity_type: str
    score: float
    engine: str
    start: Optional[int] = None
    end: Optional[int] = None
    text: Optional[str] = None
    detected: bool = True
    recognizer: str = ""
    validator: str = ""
    context_boost: bool = False
    is_proposal: bool = False
    evidence: tuple[Candidate, ...] = ()
    canonical: str = ""
    agreement: str = ""              # "" when nothing contested this span
    arbitration_rule: str = ""
    llm_verdict: str = ""            # "PII" | "NOT_PII" | "UNSURE" | ""
    llm_score: Optional[float] = None
    llm_reason: str = ""             # scrubbed

    def to_dict(self, *, return_text: bool = True) -> dict:
        d = {
            "entity_type": self.entity_type,
            "score": round(self.score, 4),
            "engine": self.engine,
            "start": self.start,
            "end": self.end,
            "detected": self.detected,
            "recognizer": self.recognizer,
            "validator": self.validator,
            "context_boost": self.context_boost,
            "is_proposal": self.is_proposal,
        }
        if self.canonical:
            d["canonical"] = self.canonical
        if self.agreement:
            d["agreement"] = self.agreement
        if self.arbitration_rule:
            d["arbitration_rule"] = self.arbitration_rule
        if self.llm_verdict:
            d["llm_verdict"] = self.llm_verdict
        if self.llm_score is not None:
            d["llm_score"] = self.llm_score
        if self.llm_reason:
            d["llm_reason"] = self.llm_reason
        if return_text:
            d["text"] = self.text
        else:
            d["text"] = ""
        return d


# Design-doc aliases (TEXT_PII_SCAN_DESIGN.md)
PIISpan = Detection


@dataclass(frozen=True)
class DetectionResult:
    """Unified scanner output for text spans or column verdicts."""

    kind: DetectionKind
    detections: tuple[Detection, ...]
    entity_counts: Mapping[str, int]
    ruleset_id: str
    ruleset_version: str
    language: str
    engines_ran: tuple[str, ...] = ()
    char_count: int = 0
    truncated: bool = False
    offset_unit: str = OFFSET_UNIT
    # Engine name -> human-readable reason it was requested but did not run
    # (e.g. NER model attached but failed to load). Distinct from an engine
    # simply not being requested, and from a valid zero-hit run.
    engines_unavailable: Mapping[str, str] = field(default_factory=dict)
    provenance_uuid: str = ""
    provenance_degraded: bool = False
    provenance_degraded_reason: str = ""
    run_uuid: str = ""
    provenance: Optional[Mapping[str, object]] = None
    coverage: Mapping[str, object] = field(default_factory=dict)
    arbitration: Mapping[str, object] = field(default_factory=dict)

    @property
    def spans(self) -> tuple[Detection, ...]:
        """Span detections only (text path convenience)."""
        if self.kind != "span":
            return ()
        return self.detections

    def to_dict(self, *, return_text: bool = True) -> dict:
        spans = [d.to_dict(return_text=return_text) for d in self.detections]
        d: dict = {
            "kind": self.kind,
            "spans": spans,
            "detections": spans,
            "entity_counts": dict(self.entity_counts),
            "ruleset_id": self.ruleset_id,
            "ruleset_version": self.ruleset_version,
            "language": self.language,
            "engines_ran": list(self.engines_ran),
            "char_count": self.char_count,
            "truncated": self.truncated,
            "offset_unit": self.offset_unit,
            "engines_unavailable": dict(self.engines_unavailable),
            "provenance_uuid": self.provenance_uuid,
            "provenance_degraded": self.provenance_degraded,
            "run_uuid": self.run_uuid,
        }
        if self.coverage:
            d["coverage"] = dict(self.coverage)
        if self.arbitration:
            d["arbitration"] = dict(self.arbitration)
        if self.provenance_degraded_reason:
            d["provenance_degraded_reason"] = self.provenance_degraded_reason
        if self.provenance:
            d["provenance"] = dict(self.provenance)
        return d


# Design-doc alias
TextScanResult = DetectionResult


@dataclass(frozen=True)
class TextScanConfig:
    """Configuration for free-text PII scanning."""

    engines: str = "both"  # "regex" | "ner" | "both" | "phone"
    language: str = "en"
    min_score: float = 0.35
    return_text: bool = True
    resolve: str = "priority"  # "priority" | "longest" | "all"
    use_llm: bool = False
    max_chars: int = 200_000
    entities: tuple[str, ...] = ()
    default_region: str = "EG"
    arabic: bool = False
    # Deterministic spoken/obfuscated expanders (Gateway default on).
    preprocess_obfuscation: bool = False
    preprocess_expanders: tuple[str, ...] = ()
    ner_window_chars: int = 1200
    ner_window_overlap: int = 200
    ner_max_windows: int = 200
    llm_window_chars: int = 3500
    llm_window_overlap: int = 300
    llm_max_windows: int = 8
    equation: str = "independent"  # same vocabulary as column decide_pii
    include_arbitration: bool = False
