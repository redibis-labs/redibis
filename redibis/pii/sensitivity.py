"""PII classification helpers — entity type -> ODCS sensitivity level."""

from __future__ import annotations

from typing import Optional

from redibis.models import (
    SENSITIVE_ENTITIES,
    INDIRECT_ENTITIES,
    SECURITY_SENSITIVE_ENTITIES,
    NON_PII_ENTITIES,
    PERSONAL_ENTITIES,
    canonical_entity,
)


def classify_sensitivity(entity_type: Optional[str]) -> str:
    """Map a detected entity_type to an ODCS classification level.

    Explicit 5-set model (no "default to personal"):
      - SENSITIVE_ENTITIES          -> "pii_sensitive"
      - INDIRECT_ENTITIES           -> "pii_indirect"
      - SECURITY_SENSITIVE_ENTITIES -> "security_sensitive"
      - PERSONAL_ENTITIES           -> "pii_personal"
      - NON_PII_ENTITIES            -> "internal"
      - None / unknown              -> "internal"  (FAIL-SAFE: unregistered entity is not
                                                    silently over-flagged as PII)

    The label is canonicalized first (GLiNER "EMAIL" -> "EMAIL_ADDRESS",
    "ADDRESS" -> "LOCATION") so both engines share one vocabulary. Any NEW
    entity_type must be registered in one of the sets in redibis.models.
    """
    et = canonical_entity(entity_type)
    if not et:
        return "internal"
    if et in SENSITIVE_ENTITIES:
        return "pii_sensitive"
    if et in INDIRECT_ENTITIES:
        return "pii_indirect"
    if et in SECURITY_SENSITIVE_ENTITIES:
        return "security_sensitive"
    if et in NON_PII_ENTITIES:
        return "internal"
    if et in PERSONAL_ENTITIES:
        return "pii_personal"
    return "internal"


def highest_sensitivity_level(classifications: list[str]) -> str:
    """Pick the strictest classification from a list of column tiers."""
    if "pii_sensitive" in classifications:
        return "pii_sensitive"
    if "pii_personal" in classifications:
        return "pii_personal"
    if "pii_indirect" in classifications:
        return "pii_indirect"
    if "security_sensitive" in classifications:
        return "security_sensitive"
    return "internal"
