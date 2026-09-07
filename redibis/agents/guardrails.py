"""Locked guardrail clauses — redibis invariants rendered as non-editable prose."""

from __future__ import annotations

GUARDRAIL_CLAUSES: list[str] = [
    "ContractStore.upsert() is the ONLY writer to the contracts bucket.",
    "Detector produces evidence; equation produces verdicts; classification engine is deterministic — LLM only proposes at judgment nodes.",
    "Memory store / column_card is format-signature-only regardless of LLM locality (inference ≠ persistence).",
    "PrivacyState = actual transformed state; the contract carries masking intent only.",
    "LawfulIntercept: detect and escalate to SecOps; NEVER apply the tag automatically.",
    "Masking enforcement is external (Ranger/Trino); redibis only annotates the contract.",
    "Generated code is proposed-not-executed: judged + scanned + approved + sandboxed.",
    "Writes and irreversible actions are policy-gated; suggest-only is the default for LLM-derived decisions.",
    "Every model call passes RAI middleware and emits an OpenTelemetry span.",
]


def render_guardrails() -> str:
    """Return locked guardrail section for prompt-plan export."""
    lines = ["The following invariants MUST hold throughout execution:"]
    for i, clause in enumerate(GUARDRAIL_CLAUSES, 1):
        lines.append(f"{i}. {clause}")
    return "\n".join(lines)
