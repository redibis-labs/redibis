"""
redibis.behavior.adapters
==========================
Engine adapters for the Behavior Policy Runtime.

Each adapter is feature-gated via ``BehaviorConfig.engine_modes``.
Import the adapter you need; the package does not eagerly import all.
"""

from redibis.behavior.adapters.pii import (
    apply_patch_to_detection,
    apply_threshold_patch,
    build_pii_context,
    build_pii_facts,
    edge_rules_to_behavior_policy,
    human_authority_for_decision,
    mark_threshold_promotions,
)

from redibis.behavior.adapters.classification import (
    ClassificationPatchResult,
    apply_patch_to_candidates,
    assert_not_forbidden,
    build_classification_context,
    engine_enabled as classification_engine_enabled,
)

from redibis.behavior.adapters.quality import (
    apply_patch_to_proposals,
    build_quality_context,
    engine_enabled as quality_engine_enabled,
)

from redibis.behavior.adapters.profiling import (
    ProfilingDirective,
    apply_patch_to_directive,
    build_profiling_context,
    engine_enabled as profiling_engine_enabled,
)

from redibis.behavior.adapters.masking import (
    ALLOWED_MASKING_STRATEGIES,
    MaskingDirective,
    apply_patch_to_plan_rule,
    build_directive_from_patch,
    build_masking_context,
    engine_enabled as masking_engine_enabled,
)

__all__ = [
    # PII
    "apply_patch_to_detection",
    "apply_threshold_patch",
    "build_pii_context",
    "build_pii_facts",
    "edge_rules_to_behavior_policy",
    "human_authority_for_decision",
    "mark_threshold_promotions",
    # Classification
    "ClassificationPatchResult",
    "apply_patch_to_candidates",
    "assert_not_forbidden",
    "build_classification_context",
    "classification_engine_enabled",
    # Quality
    "apply_patch_to_proposals",
    "build_quality_context",
    "quality_engine_enabled",
    # Profiling
    "ProfilingDirective",
    "apply_patch_to_directive",
    "build_profiling_context",
    "profiling_engine_enabled",
    # Masking
    "ALLOWED_MASKING_STRATEGIES",
    "MaskingDirective",
    "apply_patch_to_plan_rule",
    "build_directive_from_patch",
    "build_masking_context",
    "masking_engine_enabled",
]
