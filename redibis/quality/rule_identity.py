"""Backend-neutral stable identity for quality rules and results.

Any result sink (OpenMetadata, Soda, DataHub, ...) needs a deterministic id to
match a rule definition to its execution result across runs. This module is
the single source of truth for that id so no sink-specific module (e.g. the
OpenMetadata publisher) has to be imported by engine-agnostic validation code.
"""

from __future__ import annotations

import hashlib
import json
from typing import Optional


def quality_stable_rule_id(
    expectation_type: str,
    column: Optional[str],
    kwargs: Optional[dict] = None,
) -> str:
    """Short hash of (expectation_type, column, canonical kwargs).

    Deterministic across runs and processes — used to match a validated rule
    back to its contract definition and to any published result-sink record.
    """
    payload = {
        "expectation_type": expectation_type or "",
        "column": column or "",
        "kwargs": kwargs or {},
    }
    raw = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def quality_case_name(stable_id: str) -> str:
    """Redibis-namespaced result-case name, safe to reuse across sinks."""
    return f"redibis_{stable_id}"


__all__ = ["quality_stable_rule_id", "quality_case_name"]
