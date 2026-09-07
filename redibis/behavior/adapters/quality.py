"""
redibis.behavior.adapters.quality
====================================
Quality proposal behavior adapter.

Responsibilities
----------------
1. Build a BehaviorContext from quality proposals (list of expectation dicts).
2. Declare quality-specific capabilities per hook stage.
3. Apply a BehaviorPatch to a proposals list immutably.

Quality proposals are dicts with at minimum:
    expectation_type: str
    column: str (optional)
    severity: str  ("error" | "warning" | "info")
    kwargs: dict

Safety invariants
-----------------
- Durable changes to quality decisions remain in QualityDecisionStore.
  This adapter only influences proposals before they are committed.
- No raw cell values in BehaviorContext.
- Patch application returns a new list; input is never mutated.
- Suppression marks the proposal with status="suppressed" rather than
  deleting it, so audit traces remain complete.

Feature gating
--------------
Check ``behavior.engine_modes.get("quality")`` or call
``engine_enabled(cfg, "quality")`` before invoking any function here.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

from redibis.behavior.config import BehaviorConfig
from redibis.behavior.models import (
    BehaviorCapabilities,
    BehaviorContext,
    BehaviorPatch,
    HookStage,
    JSONValue,
    RuntimeMode,
)

_VALID_SEVERITIES = ("error", "warning", "info")


# ---------------------------------------------------------------------------
# Feature-gate helper
# ---------------------------------------------------------------------------

def engine_enabled(cfg: BehaviorConfig, engine: str = "quality") -> bool:
    """Return True if the quality adapter is active or shadow."""
    mode = cfg.effective_mode(engine)
    return mode in (RuntimeMode.SHADOW, RuntimeMode.ACTIVE)


# ---------------------------------------------------------------------------
# Fact extraction
# ---------------------------------------------------------------------------

def build_quality_context(
    table: str,
    proposals: Sequence[dict],
    *,
    column: Optional[str] = None,
    run_id: str = "",
    stage: HookStage = HookStage.POST_EVIDENCE,
    registry_manifest_sha256: str = "",
    environment: Optional[str] = None,
    jurisdiction: Optional[str] = None,
) -> BehaviorContext:
    """
    Build a BehaviorContext for quality proposal evaluation.

    ``proposals`` is a sequence of dicts with expectation_type / column /
    severity / kwargs. No raw values are extracted.
    """
    facts: dict[str, JSONValue] = {
        "table.name": table,
        "proposals.count": len(proposals),
    }

    if column:
        facts["column.name"] = column
        col_proposals = [p for p in proposals if p.get("column") == column]
        facts["proposals.column_count"] = len(col_proposals)
        if col_proposals:
            severities = [str(p.get("severity") or "error") for p in col_proposals]
            facts["proposals.column_severities"] = list(set(severities))
    else:
        facts["column.name"] = None

    if proposals:
        all_types = list({str(p.get("expectation_type") or "") for p in proposals if p.get("expectation_type")})
        facts["proposals.expectation_types"] = all_types
        all_severities = list({str(p.get("severity") or "error") for p in proposals})
        facts["proposals.severities"] = all_severities
        error_count = sum(1 for p in proposals if p.get("severity") == "error")
        facts["proposals.error_count"] = error_count

    caps = _quality_capabilities(stage)
    return BehaviorContext(
        run_id=run_id,
        table=table,
        column=column,
        engine="quality",
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

def _quality_capabilities(stage: HookStage) -> BehaviorCapabilities:
    if stage in (HookStage.POST_EVIDENCE, HookStage.POST_VERDICT):
        return BehaviorCapabilities(
            readable_facts=frozenset({
                "table.name", "column.name",
                "proposals.count", "proposals.column_count",
                "proposals.expectation_types", "proposals.severities",
                "proposals.column_severities", "proposals.error_count",
            }),
            allowed_actions=frozenset({
                "quality.proposal.suppress",
                "quality.proposal.change_severity",
                "core.review.require",
                "core.review.add_reason",
            }),
            allowed_patch_operations=frozenset({
                "proposal.suppress",
                "proposal.change_severity",
                "review.require",
                "review.add_reason",
            }),
            immutable_paths=frozenset(),
            value_constraints={
                "proposal.change_severity": {
                    "enum": list(_VALID_SEVERITIES),
                },
            },
        )
    return BehaviorCapabilities()


# ---------------------------------------------------------------------------
# Patch application
# ---------------------------------------------------------------------------

def apply_patch_to_proposals(
    proposals: list[dict],
    patch: BehaviorPatch,
    *,
    rule_id: str = "",
    policy_id: str = "",
    target_column: Optional[str] = None,
    target_expectation_type: Optional[str] = None,
) -> list[dict]:
    """
    Apply a BehaviorPatch to a list of quality proposals immutably.

    - ``proposal.suppress``:  marks matching proposals with status="suppressed".
    - ``proposal.change_severity``:  updates severity on matching proposals.
    - ``review.require``:  sets review_required=True on matching proposals.
    - ``review.add_reason``:  appends a behavior_reason string.

    Matching criteria: if ``target_column`` and/or ``target_expectation_type``
    are provided, only those proposals are modified. If neither is given, all
    proposals are affected.

    Input list is never mutated. Returns a new list of shallow-copied dicts.
    """
    out: list[dict] = [dict(p) for p in proposals]

    require_review = patch.require_review
    reasons = list(patch.reasons)

    # Collect review additions from ops
    for op in patch.operations:
        if op.op != "set":
            continue
        if op.path == "review.add_reason" and op.value is not None:
            reasons.append(str(op.value)[:512])
        elif op.path == "review.require":
            require_review = True

    trace = f"behavior_policy:{policy_id}:{rule_id}" if rule_id else ""

    for proposal in out:
        if not _matches_target(proposal, target_column, target_expectation_type):
            continue

        for op in patch.operations:
            if op.op != "set":
                continue

            if op.path == "proposal.suppress":
                proposal["status"] = "suppressed"
                proposal["suppressed_by_policy"] = policy_id or "behavior"
                proposal["suppressed_by_rule"] = rule_id
                if trace:
                    proposal["behavior_trace"] = trace

            elif op.path == "proposal.change_severity":
                new_sev = str(op.value or "").lower()
                if new_sev in _VALID_SEVERITIES:
                    proposal["severity"] = new_sev
                    if trace:
                        proposal["behavior_trace"] = trace

        if require_review:
            proposal["review_required"] = True
        if reasons:
            existing = proposal.get("behavior_reasons") or []
            proposal["behavior_reasons"] = list(existing) + reasons

    return out


def _matches_target(
    proposal: dict,
    target_column: Optional[str],
    target_expectation_type: Optional[str],
) -> bool:
    if target_column is not None and proposal.get("column") != target_column:
        return False
    if target_expectation_type is not None and proposal.get("expectation_type") != target_expectation_type:
        return False
    return True
