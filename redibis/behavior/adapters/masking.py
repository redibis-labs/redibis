"""
redibis.behavior.adapters.masking
====================================
Masking recommendation behavior adapter.

Responsibilities
----------------
1. Build a BehaviorContext from masking plan metadata (column + strategy info).
2. Declare masking-specific capabilities per hook stage.
3. Apply a BehaviorPatch to a masking plan column rule, returning a new rule dict.

Safety invariants
-----------------
- Keys are NEVER exposed in BehaviorContext or in patch application output.
  No field named ``key``, ``seed``, ``secret``, or similar is permitted.
- Only strategies from ``ALLOWED_MASKING_STRATEGIES`` may be selected.
- Plan parameter changes are bounded by the column rule schema.
- Enforcement remains external (Ranger/Trino); this adapter only annotates intent.
- Approval is required before masking plan changes affect the active plan:
  ``require_approval`` maps to ``review_required=True`` in the output.
- Patch application never performs I/O.

Feature gating
--------------
Check ``behavior.engine_modes.get("masking")`` or call
``engine_enabled(cfg, "masking")`` before invoking any function here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from redibis.behavior.config import BehaviorConfig
from redibis.behavior.models import (
    BehaviorCapabilities,
    BehaviorContext,
    BehaviorPatch,
    HookStage,
    JSONValue,
    RuntimeMode,
)

# Allowed masking strategy names (must match MaskingEngine._transform_series branches)
ALLOWED_MASKING_STRATEGIES = frozenset({
    "mask",
    "hash",
    "encrypt",
    "fpe",
    "fake",
    "regex",
    "suppress",
    "redact",
    "tokenize",
})

# Parameter keys that are explicitly forbidden from appearing in patch values
_FORBIDDEN_PARAM_KEYS = frozenset({
    "key", "seed", "secret", "private_key", "aes_key", "hmac_key",
    "encryption_key", "fpe_key", "salt",
})


# ---------------------------------------------------------------------------
# Feature-gate helper
# ---------------------------------------------------------------------------

def engine_enabled(cfg: BehaviorConfig, engine: str = "masking") -> bool:
    """Return True if the masking adapter is active or shadow."""
    mode = cfg.effective_mode(engine)
    return mode in (RuntimeMode.SHADOW, RuntimeMode.ACTIVE)


# ---------------------------------------------------------------------------
# Directive model
# ---------------------------------------------------------------------------

@dataclass
class MaskingDirective:
    """
    Run-scoped directive returned by the masking adapter.

    ``suggested_strategy``: proposed strategy name (from allowlist).
    ``plan_params``: additional parameters to merge into the column rule.
    ``require_approval``: when True, the column rule must be reviewed before
        the plan is applied. This maps to ``review_required=True`` in the
        updated rule dict.
    ``reasons``: human-readable audit trail.
    """
    suggested_strategy: Optional[str] = None
    plan_params: dict[str, Any] = field(default_factory=dict)
    require_approval: bool = False
    reasons: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Fact extraction
# ---------------------------------------------------------------------------

def build_masking_context(
    table: str,
    column: str,
    *,
    current_strategy: Optional[str] = None,
    entity_type: Optional[str] = None,
    column_classification: Optional[str] = None,
    run_id: str = "",
    stage: HookStage = HookStage.PRE_ACTION,
    registry_manifest_sha256: str = "",
    environment: Optional[str] = None,
    jurisdiction: Optional[str] = None,
) -> BehaviorContext:
    """
    Build a BehaviorContext for masking recommendation.

    Only strategy metadata and classification info are included.
    Keys, seeds, and secrets are NEVER included.
    """
    facts: dict[str, JSONValue] = {
        "table.name": table,
        "column.name": column,
    }

    if current_strategy:
        facts["masking.current_strategy"] = current_strategy
        facts["masking.strategy_allowed"] = current_strategy in ALLOWED_MASKING_STRATEGIES
    if entity_type:
        facts["column.entity_type"] = entity_type
    if column_classification:
        facts["column.classification"] = column_classification

    caps = _masking_capabilities(stage)
    return BehaviorContext(
        run_id=run_id,
        table=table,
        column=column,
        engine="masking",
        stage=stage,
        facts=facts,
        capabilities=caps,
        registry_manifest_sha256=registry_manifest_sha256,
        environment=environment,
        jurisdiction=jurisdiction,
    )


# ---------------------------------------------------------------------------
# Capabilities declaration
# ---------------------------------------------------------------------------

def _masking_capabilities(stage: HookStage) -> BehaviorCapabilities:
    if stage in (HookStage.PRE_ACTION, HookStage.POST_VERDICT):
        return BehaviorCapabilities(
            readable_facts=frozenset({
                "table.name", "column.name",
                "masking.current_strategy", "masking.strategy_allowed",
                "column.entity_type", "column.classification",
            }),
            allowed_actions=frozenset({
                "masking.suggest_strategy",
                "masking.set_plan_params",
                "masking.require_approval",
                "core.review.require",
                "core.review.add_reason",
            }),
            allowed_patch_operations=frozenset({
                "masking.strategy",
                "masking.plan_params",
                "masking.require_approval",
                "review.require",
                "review.add_reason",
            }),
            immutable_paths=frozenset(),
            value_constraints={
                "masking.strategy": {
                    "enum": sorted(ALLOWED_MASKING_STRATEGIES),
                },
            },
        )
    return BehaviorCapabilities()


# ---------------------------------------------------------------------------
# Guard
# ---------------------------------------------------------------------------

def _assert_no_key_params(params: dict) -> None:
    """Raise ValueError if params contain any forbidden key names."""
    forbidden = {k for k in params if k.lower() in _FORBIDDEN_PARAM_KEYS}
    if forbidden:
        raise ValueError(
            f"Masking plan params must not include key/secret fields: {sorted(forbidden)}. "
            "Keys are never exposed via behavior policy."
        )


# ---------------------------------------------------------------------------
# Patch application
# ---------------------------------------------------------------------------

def apply_patch_to_plan_rule(
    rule: dict,
    patch: BehaviorPatch,
    *,
    rule_id: str = "",
    policy_id: str = "",
) -> dict:
    """
    Apply a BehaviorPatch to a masking plan column rule dict immutably.

    Returns a new rule dict. The input ``rule`` is never mutated.

    - ``masking.strategy``:  sets ``strategy`` if in ALLOWED_MASKING_STRATEGIES.
    - ``masking.plan_params``:  merges safe params (no key/secret fields).
    - ``masking.require_approval``:  sets ``review_required=True``.
    - ``review.require``:  sets ``review_required=True``.
    - ``review.add_reason``:  appends reason string.

    Keys are never written to or read from the rule dict.
    """
    out = dict(rule)
    reasons: list[str] = list(patch.reasons)

    if patch.require_review:
        out["review_required"] = True

    trace = f"behavior_policy:{policy_id}:{rule_id}" if rule_id else ""

    for op in patch.operations:
        if op.op != "set":
            continue

        if op.path == "masking.strategy":
            strategy = str(op.value or "").lower()
            if strategy in ALLOWED_MASKING_STRATEGIES:
                out["strategy"] = strategy
                if trace:
                    out["behavior_trace"] = trace

        elif op.path == "masking.plan_params":
            if isinstance(op.value, dict):
                params: dict = op.value
                _assert_no_key_params(params)
                existing = dict(out.get("params") or {})
                existing.update(params)
                out["params"] = existing
                if trace:
                    out["behavior_trace"] = trace

        elif op.path in ("masking.require_approval", "review.require"):
            out["review_required"] = True

        elif op.path == "review.add_reason" and op.value is not None:
            reasons.append(str(op.value)[:512])

    if reasons:
        existing_reasons = list(out.get("behavior_reasons") or [])
        out["behavior_reasons"] = existing_reasons + reasons

    return out


def build_directive_from_patch(patch: BehaviorPatch) -> MaskingDirective:
    """Convenience: extract a MaskingDirective from a patch without a base rule."""
    suggested: Optional[str] = None
    plan_params: dict[str, Any] = {}
    require_approval = patch.require_review
    reasons: list[str] = list(patch.reasons)

    for op in patch.operations:
        if op.op != "set":
            continue
        if op.path == "masking.strategy":
            s = str(op.value or "").lower()
            if s in ALLOWED_MASKING_STRATEGIES:
                suggested = s
        elif op.path == "masking.plan_params":
            if isinstance(op.value, dict):
                _assert_no_key_params(op.value)
                plan_params.update(op.value)
        elif op.path in ("masking.require_approval", "review.require"):
            require_approval = True
        elif op.path == "review.add_reason" and op.value is not None:
            reasons.append(str(op.value)[:512])

    return MaskingDirective(
        suggested_strategy=suggested,
        plan_params=plan_params,
        require_approval=require_approval,
        reasons=reasons,
    )
