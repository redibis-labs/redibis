"""PII text scrubbing helpers for free-text scan artifacts.

Re-exports ``redibis.contracts.privacy.scrub_pii_text`` so scan/report code
has a stable import path that does not reach into contracts internals.
"""

from __future__ import annotations

from redibis.contracts.privacy import scrub_pii_text

__all__ = ["scrub_pii_text"]
