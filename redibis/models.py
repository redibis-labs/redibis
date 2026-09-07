"""
pii_detection.models
====================
Canonical data contracts shared across the entire package.

These are pure dataclasses with no imports from any other pii_detection module.
Every other module (ge_classes, decisions, layers, orchestrator, storage, webapp)
imports from here — never the reverse.

Public API
----------
  ColumnProfile   — triage signal per column (GE profiler → NER detector)
  PIIDetection       — detection result per column (NER detector → report writer)
  RunMetadata        — run-level metadata (orchestrator → report writer)

Constants
---------
  ARABIC_UNICODE_RE  — regex matching Arabic Unicode blocks
  PII_NAME_HINTS     — column name tokens that hint at PII presence
  SENSITIVE_ENTITIES — entity types classified as pii_sensitive (vs pii_personal)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

#: Regex matching Arabic Unicode blocks (Basic + Supplement + Extended-A).
ARABIC_UNICODE_RE: str = r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]+"

EQUATION_VOTER_IDS = (
    "regex", "ner", "llm", "phone", "learned", "nid", "imei", "imsi", "geo",
)


def arabic_regex_for_ge() -> str:
    """
    GE's regex engine rejects ``\\u`` escapes; build literal Unicode range endpoints.
    """
    return (
        "["
        f"{chr(0x0600)}-{chr(0x06FF)}"
        f"{chr(0x0750)}-{chr(0x077F)}"
        f"{chr(0x08A0)}-{chr(0x08FF)}"
        "]+"
    )

#: Column name tokens (EN + AR) that hint at PII presence.
PII_NAME_HINTS: tuple[str, ...] = (
    "name", "phone", "mobile", "email", "address", "national",
    "passport", "birth", "gender", "id", "nid", "iban", "card",
    "الاسم", "رقم", "هاتف", "عنوان", "جنسية", "بريد", "هوية",
    "ميلاد", "جوال",
)

#: Entity types that map to the ``pii_sensitive`` classification level.
SENSITIVE_ENTITIES: frozenset[str] = frozenset({
    "EG_NATIONAL_ID", "NATIONAL_ID", "PASSPORT",
    "CREDIT_CARD", "CRYPTO_WALLET", "GDPR_SPECIAL_CATEGORY",
    "EG_TAX_ID", "IBAN_CODE",
    # secrets / credentials — higher risk than plain "personal"
    "OTP", "SIM_PUK",
})

#: Entity types that map to the ``pii_indirect`` classification level.
#: ``EMPLOYER`` is an organization name too, but *in an employer/workplace
#: column* it indirectly identifies the specific person who works there --
#: unlike a bare ``ORGANIZATION`` reference (e.g. "customer owns a Samsung
#: device"), which stays "internal" (see ``NON_PII_ENTITIES``).
INDIRECT_ENTITIES: frozenset[str] = frozenset({
    "SESSION_ID", "PSEUDO_ID", "DEVICE_ID", "COOKIE_ID", "TRACKING_ID", "IP_ADDRESS",
    "EMPLOYER",
})

#: Entity types that map to the ``security_sensitive`` classification level.
SECURITY_SENSITIVE_ENTITIES: frozenset[str] = frozenset({
    "PASSWORD_HASH", "CSRF_TOKEN", "API_KEY", "SECRET", "JWT", "SESSION_TOKEN",
})

#: Entity types that are NOT personal data -> classify as "internal" (non-PII).
#: A SWIFT/BIC identifies a *bank*; an organization name is a *company*; a bare
#: content hash is a checksum -- none are data about a natural person.
NON_PII_ENTITIES: frozenset[str] = frozenset({
    "SWIFT_BIC", "ORGANIZATION", "CONTENT_HASH", "URL",
    "NETWORK_ID", "CELL_ID",
})

#: Personally-identifiable but lower-risk entity types -> "pii_personal".
#: Listed EXPLICITLY so an unknown/unregistered entity type fails safe to
#: "internal" (non-PII) rather than being silently over-flagged as PII.
PERSONAL_ENTITIES: frozenset[str] = frozenset({
    "PHONE_NUMBER", "EMAIL_ADDRESS", "PERSON", "DATE_TIME", "GENDER",
    "LOCATION", "MAC_ADDRESS", "IMEI", "IMSI", "ICCID",
    "UTILITY_ACCOUNT", "EG_VEHICLE_PLATE", "SOCIAL_PROFILE_URL",
})

#: Normalize engine-specific aliases (GLiNER vs regex) to ONE canonical vocabulary
#: so masking policy, contract tags, and the column fingerprint see one entity name.
CANONICAL_ENTITY: dict[str, str] = {
    "EMAIL": "EMAIL_ADDRESS",
    "ADDRESS": "LOCATION",
    "PHONE": "PHONE_NUMBER",
    "MSISDN": "PHONE_NUMBER",
    "ORG": "ORGANIZATION",
    "SOCIAL_PROFILE_URL": "SOCIAL_PROFILE_URL",
    "URL": "URL",
    "SESSION_ID": "SESSION_ID",
    "PSEUDO_ID": "PSEUDO_ID",
    "DEVICE_ID": "DEVICE_ID",
    "COOKIE_ID": "COOKIE_ID",
    "TRACKING_ID": "TRACKING_ID",
    "PASSWORD_HASH": "PASSWORD_HASH",
    "CSRF_TOKEN": "CSRF_TOKEN",
    "API_KEY": "API_KEY",
    "SECRET": "SECRET",
    "JWT": "JWT",
    "SESSION_TOKEN": "SESSION_TOKEN",
    "SID": "SESSION_ID",
    "SESSION": "SESSION_ID",
    "CSRF": "CSRF_TOKEN",
    "XSRF": "CSRF_TOKEN",
    "PASSWORD": "PASSWORD_HASH",
    "PASSWD": "PASSWORD_HASH",
    "PWD": "PASSWORD_HASH",
    "PWD_HASH": "PASSWORD_HASH",
    "JWT_TOKEN": "JWT",
}


def canonical_entity(entity_type: Optional[str]) -> Optional[str]:
    """Map an engine-specific entity label to the canonical vocabulary."""
    if not entity_type:
        return entity_type
    return CANONICAL_ENTITY.get(entity_type, entity_type)


#: Map the internal classification levels to an enterprise label vocabulary.
ENTERPRISE_CLASSIFICATION: dict[str, str] = {
    "internal":      "internal",
    "pii_personal":  "confidential",
    "pii_sensitive": "restricted",
}


def __getattr__(name: str):
    """Lazy re-exports — policy helpers moved to domain modules (one release)."""
    if name == "classify_sensitivity":
        from redibis.pii.sensitivity import classify_sensitivity
        return classify_sensitivity
    if name == "suggest_masking_default":
        from redibis.contracts.masking_policy import suggest_masking_default
        return suggest_masking_default
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ─────────────────────────────────────────────────────────────────────────────
# Data Contracts
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ColumnProfile:
    """
    Triage signal for a single column, produced by the GE profiler.
    Consumed by the NER detector to decide which columns to scan.

    All numeric fields are rounded at construction time by the profiler.
    This object is read-only after creation.
    """
    column:            str
    dtype:             str
    cardinality_ratio: float
    avg_value_length:  float
    null_rate:         float
    name_hint_score:   float
    arabic_fraction:   float
    triage_score:      float
    send_to_detector:  bool             # True → pass to NER/Presidio
    physical_type:     str = "string"
    logical_type:      str = "string"
    type_source:       str = "pandas"   # pandas | great_expectations | open_metadata
    sample_values:     List[str] = field(default_factory=list)
    constant:          bool = False
    near_constant:     bool = False
    redundant_with:    List[str] = field(default_factory=list)

    def __repr__(self) -> str:
        flag = "SCAN" if self.send_to_detector else "skip"
        return (
            f"ColumnProfile({self.column!r} | {flag} | "
            f"score={self.triage_score:.2f} | arabic={self.arabic_fraction:.2f})"
        )


@dataclass
class PIIDetection:
    """
    Detection result for a single column.

    Produced by the NER detector (Presidio + GLiNER + optional LLM).
    Consumed by the equation module (decide_pii) and the report writer.

    Contract rules
    --------------
    - ``detected`` is always False when leaving the NER detector.
      The orchestrator applies ``decide_pii()`` to set the final verdict.
    - Per-engine scores are raw — do not apply thresholds inside the detector.
    - New engines add new fields here; do NOT change existing field semantics.
    """
    column:              str
    detected:            bool                # final verdict — set by decide_pii()
    entity_type:         Optional[str]       = None
    confidence:          float               = 0.0        # 0–1, set by decide_pii()
    equation_used:       str                 = "independent"
    decision_path:       str                 = ""         # human-readable trace

    # ── Per-engine raw scores ─────────────────────────────────────────────
    presidio_score:      Optional[float]     = None
    presidio_pattern:    Optional[str]       = None       # catalog pattern key
    presidio_match_rate: Optional[float]     = None       # fraction of values matched
    gliner_score:        Optional[float]     = None       # NER engine score (name kept for compat)
    gliner_label:        Optional[str]       = None       # NER engine label
    gliner_match_rate:   Optional[float]     = None
    ner_engine:          Optional[str]       = None       # e.g. gliner:/models/my-ner
    llm_score:           Optional[float]     = None
    llm_verdict:         Optional[str]       = None       # CONFIRMED|REJECTED|UNCERTAIN
    llm_reasoning:       Optional[str]       = None
    phone_score:         Optional[float]     = None       # MSISDN plan / libphonenumber
    phone_entity:        Optional[str]       = None
    msisdn_valid_rate:   Optional[float]     = None
    phone_valid_rate:    Optional[float]     = None
    phone_mobile_rate:   Optional[float]     = None
    phone_regions:       Optional[dict]      = None
    # Egyptian National ID column-level evidence (from national_id_egypt.nid_rates).
    nid_valid_rate:      Optional[float]     = None
    nid_checked:         int                 = 0
    nid_age_p05:         Optional[float]     = None
    nid_age_p95:         Optional[float]     = None
    nid_gov_distinct:    Optional[int]       = None
    nid_monotonic_rate:  Optional[float]     = None
    nid_unique_rate:     Optional[float]     = None
    # Device / subscriber / geo column-level evidence.
    imei_valid_rate:     Optional[float]     = None
    imei_checked:        int                 = 0
    imsi_valid_rate:     Optional[float]     = None
    imsi_checked:        int                 = 0
    geo_confidence:      Optional[float]     = None
    # Structured learned classifier (not NER — do not overload gliner_*).
    learned_score:       Optional[float]     = None
    learned_label:       Optional[str]       = None
    learned_entity:      Optional[str]       = None
    learned_engine:      Optional[str]       = None       # e.g. learned:gbt-v1

    # ── Column context ────────────────────────────────────────────────────
    arabic_aware:        bool                = False
    arabic_fraction:     float               = 0.0
    triage_score:        float               = 0.0        # GE triage score
    sample_match_rate:   Optional[float]     = None

    # ── GE expectation hint ───────────────────────────────────────────────
    regex_pattern:       Optional[str]       = None       # raw regex for GE expect
    suggested_mostly:    float               = 0.80       # GE 'mostly' threshold

    # Multi-entity evidence (all fired patterns / NER passes).
    # Field shape owned by PII_SCAN_LOGGING_AND_EXPORT_PLAN.md — NER modular layer populates only.
    regex_hits:          List[dict]          = field(default_factory=list)
    ner_hits:            List[dict]          = field(default_factory=list)
    engine_states:       Dict[str, dict]     = field(default_factory=dict)
    # Plugin-neutral pre-verdict map (engine_id → EngineEvidenceRecord dict).
    engine_evidence:     Dict[str, dict]     = field(default_factory=dict)

    # ── Edge-rule carry-forward metadata ──────────────────────────────────
    # Populated by refine_detection() for downstream classification/masking stages.
    # These fields are NEVER raw values; they carry policy-derived intent only.
    edge_tags:           List[str]           = field(default_factory=list)
    edge_classification: Optional[str]       = None   # e.g. "pii_sensitive"
    edge_masking_policy: Optional[str]       = None   # e.g. "hash"
    # Namespaced custom-rule IDs (``cr.<policy_name>.<rule_id>``) that matched this
    # column, resolving into ``header.rules.custom_rules`` in the evidence bundle.
    edge_rule_ids:       List[str]           = field(default_factory=list)

    @property
    def classification(self) -> str:
        """ODCS classification level derived from entity_type."""
        from redibis.pii.sensitivity import classify_sensitivity
        return classify_sensitivity(self.entity_type)

    def contributing_engines(self) -> List[str]:
        """Returns names of engines that produced evidence for this column."""
        engines = []
        if self.presidio_score is not None:
            engines.append("regex")
        if self.gliner_score is not None:
            engines.append("ner")
        if self.llm_score is not None:
            engines.append("llm")
        if self.phone_score is not None:
            engines.append("phone")
        if self.learned_score is not None:
            engines.append("learned")
        if self.nid_valid_rate is not None:
            engines.append("nid")
        if self.imei_valid_rate is not None:
            engines.append("imei")
        if self.imsi_valid_rate is not None:
            engines.append("imsi")
        if self.geo_confidence is not None:
            engines.append("geo")
        extra = self.engine_evidence or {}
        for engine_id, block in extra.items():
            if engine_id in engines or not isinstance(block, dict):
                continue
            if block.get("ran") and block.get("score") is not None:
                engines.append(str(engine_id))
        return engines

    def deciding_engines(self) -> List[str]:
        """Engines that participate in ``decide_pii`` (plugins are excluded)."""
        voters = set(EQUATION_VOTER_IDS)
        return [name for name in self.contributing_engines() if name in voters]

    # Alias for backward compatibility
    detection_engines = contributing_engines


@dataclass
class PIIColumnReport:
    """
    The formal v2 per-column PII confidence model (architecture §1.5).

    Captures every engine's confidence, a conclusion (final verdict +
    score + which engine drove it), and the human-readable decision rule
    that fired. Triage score is deliberately NOT part of this report — it
    is a quality-profiler signal exposed only via API/CLI and never
    participates in the PII verdict.
    """
    column:          str
    # per-engine confidence (None = engine did not run / produced no score)
    regex:           Optional[float] = None      # Presidio
    ner:             Optional[float] = None      # GLiNER
    llm:             Optional[float] = None      # optional refiner
    phone:           Optional[float] = None      # libphonenumber / MSISDN module
    entity_type:     Optional[str]   = None      # EMAIL | PHONE | PERSON | ...
    # conclusion
    is_pii:          bool            = False      # final verdict
    confidence:      float           = 0.0        # the conclusion score
    basis:           str             = ""         # which engine drove the verdict
    decision_rule:   str             = ""         # e.g. "regex >= 0.80 OR ner >= 0.70"
    equation:        str             = "independent"
    arabic_aware:    bool            = False
    arabic_fraction: Optional[float] = None
    engine_state:    Dict[str, Dict[str, Any]] = field(default_factory=dict)
    advisory:        Optional[str]   = None

    def to_dict(self) -> dict:
        return {
            "column": self.column,
            "engines": {
                "regex": self.regex,
                "ner": self.ner,
                "llm": self.llm,
                "phone": self.phone,
            },
            "engine_state": self.engine_state,
            "entity_type": self.entity_type,
            "conclusion": {
                "is_pii": self.is_pii,
                "confidence": self.confidence,
                "basis": self.basis,
            },
            "decision_rule": self.decision_rule,
            "equation": self.equation,
            "arabic_aware": self.arabic_aware,
            "arabic_fraction": self.arabic_fraction,
            "advisory": self.advisory,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PIIColumnReport":
        engines = d.get("engines", {}) or {}
        conclusion = d.get("conclusion", {}) or {}
        return cls(
            column=d.get("column", ""),
            regex=engines.get("regex"),
            ner=engines.get("ner"),
            llm=engines.get("llm"),
            phone=engines.get("phone"),
            entity_type=d.get("entity_type"),
            is_pii=bool(conclusion.get("is_pii", False)),
            confidence=float(conclusion.get("confidence", 0.0) or 0.0),
            basis=conclusion.get("basis", ""),
            decision_rule=d.get("decision_rule", ""),
            equation=d.get("equation", "independent"),
            arabic_aware=bool(d.get("arabic_aware", False)),
            arabic_fraction=d.get("arabic_fraction"),
            engine_state=d.get("engine_state", {}) or {},
            advisory=d.get("advisory"),
        )


@dataclass
class RunMetadata:
    """
    Run-level metadata written to the ODCS ``pii_summary`` block.
    Constructed by the orchestrator and passed into the report writer.
    """
    run_id:               str               = ""
    scan_date:            str               = ""
    equation_used:        str               = "independent"
    table_physical_name:  str               = ""
    enhancement_status:   Dict[str, bool]   = field(
        default_factory=lambda: {
            "llm_refiner_run":     False,
            "llm_refiner_planned": True,
        }
    )
    # Ordered pack layers that shaped this run (id / version / checksum).
    pack_layers:          List[Dict[str, Any]] = field(default_factory=list)


# ── Backward-compat aliases (deprecated — will be removed in v2.0) ─────────
PIIColumnProfile = ColumnProfile