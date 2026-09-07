"""
redibis.memory.redaction — consent-gated sample redaction for the memory store.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol

from redibis.memory.fingerprint import value_to_shape


class SamplingConsentLike(Protocol):
    def is_approved(self, table: str, column: str) -> bool: ...


def memory_safe_shape(value: Any) -> str:
    """Length + token-shape mask — never includes raw digits or characters."""
    text = str(value)
    return f"n{len(text)}:{value_to_shape(text)}"


def redact_samples(
    values: list[Any],
    *,
    table: str,
    column: str,
    consent: Optional[SamplingConsentLike] = None,
    approved: Optional[bool] = None,
) -> list[str]:
    """
    Return redacted sample strings safe for the memory store.

    With or without consent, only format-preserving shape masks are returned —
    never raw values, unsalted digests, or reversible tokens. Consent controls
    whether the steward has approved persisting sample shapes for this column
    (the writer may still omit them when consent is denied).
    """
    clean = [str(v) for v in values if v is not None and str(v).strip()]
    if not clean:
        return []

    is_approved = approved
    if is_approved is None and consent is not None:
        is_approved = consent.is_approved(table, column)

    # Shape masks only — consent does not escalate to hashes or raw-adjacent data.
    limit = 8 if is_approved else 5
    return list({memory_safe_shape(v) for v in clean[:limit]})
