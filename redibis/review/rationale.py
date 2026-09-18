"""Closed rationale vocabulary for steward field verdicts.

Free text cannot be aggregated, cannot become few-shot examples, and cannot
train anything. Codes here are the only allowed ``rationale_code`` values;
``other`` requires ``rationale_text``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RationaleCode:
    code: str
    label: str
    description: str


RATIONALE_CODES: tuple[RationaleCode, ...] = (
    RationaleCode("engine_correct", "Engine correct",
                  "The chosen engine's opinion matches how this organisation classifies the field."),
    RationaleCode("engine_wrong_type", "Engine wrong type",
                  "The engine detected PII (or a type) but named the wrong entity."),
    RationaleCode("engine_wrong_boundary", "Engine wrong boundary",
                  "The engine's span or column boundary is too wide or too narrow."),
    RationaleCode("false_positive_quantity", "False positive (quantity)",
                  "A numeric or monetary quantity was mistaken for an identifier."),
    RationaleCode("false_positive_identifier_not_personal", "Identifier, not personal",
                  "An identifier fired, but it is not personal data in this context."),
    RationaleCode("business_definition_supplied", "Business definition supplied",
                  "A steward-authored business definition is the source of truth."),
    RationaleCode("glossary_term_applied", "Glossary term applied",
                  "An organisation glossary term was applied to this column."),
    RationaleCode("classification_policy", "Classification policy",
                  "Organisation classification policy dictates this level."),
    RationaleCode("regulatory_requirement", "Regulatory requirement",
                  "A regulation or internal control requires this treatment."),
    RationaleCode("sample_inspection", "Sample inspection",
                  "The steward inspected consented samples and decided from them."),
    RationaleCode("domain_knowledge", "Domain knowledge",
                  "Steward domain knowledge about this table, not visible in samples."),
    RationaleCode("duplicate_of_column", "Duplicate of column",
                  "This column is a duplicate or alias of another reviewed column."),
    RationaleCode("deprecated_column", "Deprecated column",
                  "The column is deprecated / unused and left as-is."),
    RationaleCode("other", "Other",
                  "None of the closed codes fit; rationale_text is required."),
)

RATIONALE_CODE_SET: frozenset[str] = frozenset(c.code for c in RATIONALE_CODES)
RATIONALE_BY_CODE: dict[str, RationaleCode] = {c.code: c for c in RATIONALE_CODES}

#: Codes that make sense when the steward overrules an engine generation.
ENGINE_OVERRIDE_CODES: frozenset[str] = frozenset({
    "engine_wrong_type",
    "engine_wrong_boundary",
    "false_positive_quantity",
    "false_positive_identifier_not_personal",
    "classification_policy",
    "regulatory_requirement",
    "sample_inspection",
    "domain_knowledge",
    "other",
})

#: Codes offered when the steward accepts an engine generation.
ENGINE_ACCEPT_CODES: frozenset[str] = frozenset({
    "engine_correct",
    "sample_inspection",
    "domain_knowledge",
    "classification_policy",
    "regulatory_requirement",
    "glossary_term_applied",
    "other",
})


class RationaleError(ValueError):
    """Invalid rationale_code / missing rationale_text."""


def validate_rationale(code: str, text: str = "") -> str:
    """Return a normalised code or raise ``RationaleError``."""
    normalised = (code or "").strip()
    if normalised not in RATIONALE_CODE_SET:
        raise RationaleError(
            f"rationale_code must be one of {sorted(RATIONALE_CODE_SET)}"
        )
    if normalised == "other" and not (text or "").strip():
        raise RationaleError("rationale_text is required when rationale_code is 'other'")
    return normalised


def codes_for_choice(chosen_source: str, *, agreement: str = "") -> list[str]:
    """Rationale codes that make sense for a chosen generation source.

    When ``agreement`` is ``no_evidence`` there is no engine to accept, so
    engine-accept codes (``engine_correct``, …) are hidden.
    """
    source = (chosen_source or "").strip().lower()
    if source in ("human",):
        codes = set(ENGINE_OVERRIDE_CODES)
    elif source in ("regex", "ner", "phone", "llm", "llm_synthesis", "custom_rule", "profile"):
        codes = set(ENGINE_ACCEPT_CODES)
    else:
        codes = set(RATIONALE_CODE_SET)
    if agreement == "no_evidence":
        codes -= ENGINE_ACCEPT_CODES - ENGINE_OVERRIDE_CODES
        codes.discard("engine_correct")
    return sorted(codes)


def as_dicts() -> list[dict[str, str]]:
    return [
        {"code": c.code, "label": c.label, "description": c.description}
        for c in RATIONALE_CODES
    ]
