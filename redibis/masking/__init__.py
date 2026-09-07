"""
redibis.masking — Data tab de-identification pipeline (v1).

Produces a safe-to-share copy of a dataset: real PII (names, phones, emails,
addresses, IDs…) replaced with realistic-but-fake values, so the data can be
handed to a colleague or an external LLM without leaking real PII.

Public API:
    MaskingPlan, ColumnMaskRule   — the (saveable, reusable) plan object model
    auto_suggest_plan, suggest_rule — pre-fill a plan from a PII scan
    MaskingEngine, RunKeys        — apply a plan with per-run keys
    risk_report                   — flag weak de-identification choices
    transforms                    — value-level primitives + capabilities()
"""

from redibis.masking.plan import (
    MaskingPlan, ColumnMaskRule, STRATEGIES,
    auto_suggest_plan, suggest_rule,
)
from redibis.masking.engine import MaskingEngine, RunKeys, risk_report
from redibis.masking import transforms
from redibis.masking.regex_library import (
    load_regex_library, resolve_pattern, list_patterns, clear_cache,
)

__all__ = [
    "MaskingPlan", "ColumnMaskRule", "STRATEGIES",
    "auto_suggest_plan", "suggest_rule",
    "MaskingEngine", "RunKeys", "risk_report", "transforms",
    "load_regex_library", "resolve_pattern", "list_patterns", "clear_cache",
]
