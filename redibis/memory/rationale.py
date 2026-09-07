"""
Auto-derived rationales for review decisions (human-overridable later).
"""

from __future__ import annotations

from typing import Any, Optional

from redibis.memory.decision import ReviewDecision
from redibis.memory.fingerprint import ColumnFingerprint


def derive_rationale(
    decision: ReviewDecision,
    fingerprint: ColumnFingerprint,
) -> str:
    """Template rationale from the decision, fingerprint evidence, and samples."""
    parts: list[str] = []

    fmt = fingerprint.format_signature.value
    ltype = fingerprint.logical_type.value
    parts.append(
        f"Column '{fingerprint.name_normalized}' ({ltype}, format={fmt})"
    )

    if fingerprint.entity_type:
        parts.append(f"detection evidence suggested entity '{fingerprint.entity_type}'")

    if fingerprint.redacted_samples:
        shapes = ", ".join(fingerprint.redacted_samples[:3])
        parts.append(f"sample shapes: {shapes}")

    if decision.pii_verdict:
        parts.append(f"steward PII verdict: {decision.pii_verdict}")
    if decision.classification:
        parts.append(f"classification: {decision.classification}")
    if decision.masking_strategy:
        strat = decision.masking_strategy.get("strategy") or decision.masking_strategy
        parts.append(f"masking: {strat}")
    if decision.quality_rules:
        parts.append(f"approved {len(decision.quality_rules)} quality rule(s)")
    if decision.business_definition:
        snippet = decision.business_definition.strip()
        if len(snippet) > 120:
            snippet = snippet[:117] + "..."
        parts.append(f"business definition: {snippet}")

    if decision.provenance.get("workflow"):
        parts.append(f"via {decision.provenance['workflow']}")

    return "; ".join(parts) + "."


def apply_rationale(
    decision: ReviewDecision,
    fingerprint: ColumnFingerprint,
    *,
    override: Optional[str] = None,
) -> ReviewDecision:
    if override:
        decision.rationale = override
    elif not decision.rationale:
        decision.rationale = derive_rationale(decision, fingerprint)
    return decision
