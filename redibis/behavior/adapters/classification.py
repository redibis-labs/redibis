"""
redibis.behavior.adapters.classification
==========================================
Classification engine behavior adapter.

Responsibilities
----------------
1. Build a privacy-safe BehaviorContext from classification candidate state.
2. Declare classification-specific capabilities per hook stage.
3. Apply a reduced BehaviorPatch to a candidate list immutably.
4. Guard against forbidden tag application.

Safety invariants
-----------------
- apply_forbidden / golden rules are NEVER bypassed by this adapter.
  ``assert_not_forbidden(tag, policy)`` raises ValueError on any attempt.
- Raw cell values are never present in BehaviorContext.
- Patch application is immutable (no in-place mutation of candidate lists).
- Human-decision overlays outrank any automated policy patch.
- The adapter does not write to the contracts bucket.

Feature gating
--------------
Check ``behavior.engine_modes.get("classification")`` or call
``engine_enabled(cfg, "classification")`` before invoking any function here.
The pipeline wrapper is responsible for gating; individual functions do not
check config so they remain testable in isolation.

Integration point
-----------------
Called by the classification pipeline step when behavior.enabled=True and
behavior.engine_modes["classification"] in (shadow, active).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from redibis.behavior.config import BehaviorConfig
from redibis.behavior.models import (
    Authority,
    BehaviorCapabilities,
    BehaviorContext,
    BehaviorPatch,
    HookStage,
    JSONValue,
    PatchOperation,
    RuntimeMode,
)


# ---------------------------------------------------------------------------
# Feature-gate helper
# ---------------------------------------------------------------------------

def engine_enabled(cfg: BehaviorConfig, engine: str = "classification") -> bool:
    """Return True if the classification adapter is active or shadow."""
    mode = cfg.effective_mode(engine)
    return mode in (RuntimeMode.SHADOW, RuntimeMode.ACTIVE)


# ---------------------------------------------------------------------------
# Forbidden-tag guard
# ---------------------------------------------------------------------------

def assert_not_forbidden(tag: str, policy: Any) -> None:
    """
    Raise ValueError if ``tag`` is on the policy's forbidden list.

    Checks ``forbidden_tags`` when present, then per-tag ``TagSpec.apply_forbidden``
    via ``policy.tag_spec(domain, tag)`` / ``policy.tags`` — the ClassificationPolicy
    surface used by PolicyEngine. Never bypassed.
    """
    if policy is None:
        return
    forbidden = getattr(policy, "forbidden_tags", None) or []
    if tag in forbidden:
        raise ValueError(
            f"Behavior policy may not add forbidden tag '{tag}'. "
            "The apply_forbidden guard is never bypassed."
        )

    # TagSpec.apply_forbidden (policy_pack) — authoritative for LI / SecOps tags
    tag_spec_fn = getattr(policy, "tag_spec", None)
    if callable(tag_spec_fn):
        # tag may be "domain:tag" or bare tag
        domain, _, bare = tag.partition(":")
        if bare:
            spec = tag_spec_fn(domain, bare)
        else:
            # Search known domains
            specs = getattr(policy, "tags", None) or {}
            spec = None
            for _dom, by_tag in (specs.items() if isinstance(specs, dict) else []):
                if isinstance(by_tag, dict) and tag in by_tag:
                    spec = by_tag[tag]
                    break
            if spec is None:
                # try empty/common domain lookup
                for domain_name in ("security", "privacy", "telecom", "data"):
                    spec = tag_spec_fn(domain_name, tag)
                    if spec is not None:
                        break
        if spec is not None and bool(getattr(spec, "apply_forbidden", False)):
            raise ValueError(
                f"Behavior policy may not add forbidden tag '{tag}'. "
                "TagSpec.apply_forbidden is never bypassed."
            )

    apply_forbidden = getattr(policy, "apply_forbidden", None)
    if callable(apply_forbidden):
        apply_forbidden(tag)


# ---------------------------------------------------------------------------
# Fact extraction
# ---------------------------------------------------------------------------

def build_classification_context(
    table: str,
    column: str,
    candidates: Sequence[dict],
    *,
    run_id: str = "",
    stage: HookStage = HookStage.POST_VERDICT,
    registry_manifest_sha256: str = "",
    environment: Optional[str] = None,
    jurisdiction: Optional[str] = None,
    column_profile: Any = None,
) -> BehaviorContext:
    """
    Build a BehaviorContext for a classification candidate list.

    ``candidates`` is a list of dicts with keys such as:
        tag, score, source, source_model, confidence

    No raw cell values are included. Aggregate scores only.
    """
    facts: dict[str, JSONValue] = {
        "column.name": column,
        "table.name": table,
        "candidates.count": len(candidates),
    }

    if candidates:
        scores = [float(c.get("score") or c.get("confidence") or 0.0) for c in candidates]
        facts["candidates.max_score"] = max(scores)
        facts["candidates.min_score"] = min(scores)
        facts["candidates.avg_score"] = sum(scores) / len(scores)
        tags = [str(c.get("tag") or "") for c in candidates if c.get("tag")]
        if tags:
            facts["candidates.tags"] = tags

    if column_profile is not None:
        null_rate = getattr(column_profile, "null_rate", None)
        if null_rate is not None:
            facts["profile.null_rate"] = float(null_rate)
        logical_type = getattr(column_profile, "logical_type", None)
        if logical_type:
            facts["column.logical_type"] = str(logical_type)

    caps = _classification_capabilities(stage)
    return BehaviorContext(
        run_id=run_id,
        table=table,
        column=column,
        engine="classification",
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

def _classification_capabilities(stage: HookStage) -> BehaviorCapabilities:
    if stage == HookStage.POST_EVIDENCE:
        return BehaviorCapabilities(
            readable_facts=frozenset({
                "column.name", "table.name",
                "candidates.count", "candidates.max_score",
                "candidates.min_score", "candidates.avg_score",
                "candidates.tags",
                "column.logical_type", "profile.null_rate",
            }),
            allowed_actions=frozenset({
                "classification.candidate.add_tag",
                "classification.candidate.drop_tag",
                "core.review.require",
                "core.review.add_reason",
            }),
            allowed_patch_operations=frozenset({
                "candidate.add_tag",
                "candidate.drop_tag",
                "review.require",
                "review.add_reason",
            }),
            immutable_paths=frozenset(),
        )
    if stage == HookStage.POST_VERDICT:
        return BehaviorCapabilities(
            readable_facts=frozenset({
                "column.name", "table.name",
                "candidates.count", "candidates.max_score",
                "candidates.tags",
                "column.logical_type", "profile.null_rate",
            }),
            allowed_actions=frozenset({
                "classification.candidate.add_tag",
                "classification.candidate.drop_tag",
                "core.review.require",
                "core.review.add_reason",
            }),
            allowed_patch_operations=frozenset({
                "candidate.add_tag",
                "candidate.drop_tag",
                "review.require",
                "review.add_reason",
            }),
            immutable_paths=frozenset(),
        )
    if stage == HookStage.POST_ACTION:
        return BehaviorCapabilities(
            readable_facts=frozenset({
                "column.name", "table.name", "candidates.tags",
            }),
            allowed_actions=frozenset({
                "core.review.require",
                "core.review.add_reason",
            }),
            allowed_patch_operations=frozenset({
                "review.require",
                "review.add_reason",
            }),
            immutable_paths=frozenset(),
        )
    return BehaviorCapabilities()


# ---------------------------------------------------------------------------
# Patch application
# ---------------------------------------------------------------------------

@dataclass
class ClassificationPatchResult:
    """Result of applying a behavior patch to a candidate list."""
    candidates: list[dict]
    require_review: bool
    reasons: list[str]
    added_tags: list[str]
    dropped_tags: list[str]


def apply_patch_to_candidates(
    candidates: list[dict],
    patch: BehaviorPatch,
    *,
    policy: Any = None,
    rule_id: str = "",
    policy_id: str = "",
) -> ClassificationPatchResult:
    """
    Apply a BehaviorPatch to a candidate tag list immutably.

    - ``candidate.add_tag``: appends a new candidate entry (tag + reason).
      Calls ``assert_not_forbidden`` before adding.
    - ``candidate.drop_tag``: removes candidates with matching tag.
    - ``review.require``: sets require_review flag.
    - ``review.add_reason``: accumulates a reason string.

    apply_forbidden is never bypassed. If forbidden tag is requested, raises.
    Returns a ClassificationPatchResult with a new candidate list copy.
    """
    result_candidates: list[dict] = list(candidates)
    require_review = patch.require_review
    reasons: list[str] = list(patch.reasons)
    added_tags: list[str] = []
    dropped_tags: list[str] = []

    for op in patch.operations:
        if op.op != "set":
            continue

        if op.path == "candidate.add_tag":
            tag = str(op.value) if op.value is not None else ""
            if tag:
                assert_not_forbidden(tag, policy)
                result_candidates.append({
                    "tag": tag,
                    "score": 1.0,
                    "source": "behavior_policy",
                    "policy_id": policy_id,
                    "rule_id": rule_id,
                    "reason": op.reason,
                })
                added_tags.append(tag)

        elif op.path == "candidate.drop_tag":
            tag = str(op.value) if op.value is not None else ""
            if tag:
                before = len(result_candidates)
                result_candidates = [c for c in result_candidates if c.get("tag") != tag]
                if len(result_candidates) < before:
                    dropped_tags.append(tag)

        elif op.path == "review.require":
            require_review = True

        elif op.path == "review.add_reason":
            if op.value is not None:
                reasons.append(str(op.value)[:512])

    return ClassificationPatchResult(
        candidates=result_candidates,
        require_review=require_review,
        reasons=reasons,
        added_tags=added_tags,
        dropped_tags=dropped_tags,
    )
