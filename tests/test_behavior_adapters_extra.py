"""
Phase 8 adapter capability boundary tests.

Tests for: classification, quality, profiling, masking adapters.
Each test verifies capability boundaries — what operations are allowed,
what is rejected/guarded, and that immutability is preserved.
"""

from __future__ import annotations

import pytest

from redibis.behavior.adapters.classification import (
    ClassificationPatchResult,
    apply_patch_to_candidates,
    assert_not_forbidden,
    build_classification_context,
    engine_enabled as classification_engine_enabled,
)
from redibis.behavior.adapters.masking import (
    ALLOWED_MASKING_STRATEGIES,
    MaskingDirective,
    apply_patch_to_plan_rule,
    build_directive_from_patch,
    build_masking_context,
    engine_enabled as masking_engine_enabled,
)
from redibis.behavior.adapters.profiling import (
    ProfilingDirective,
    _ALLOWED_PROFILERS,
    apply_patch_to_directive,
    build_profiling_context,
    engine_enabled as profiling_engine_enabled,
)
from redibis.behavior.adapters.quality import (
    apply_patch_to_proposals,
    build_quality_context,
    engine_enabled as quality_engine_enabled,
)
from redibis.behavior.config import BehaviorConfig
from redibis.behavior.models import (
    Authority,
    BehaviorPatch,
    HookStage,
    PatchOperation,
    RuntimeMode,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _op(path: str, value=None, op: str = "set") -> PatchOperation:
    return PatchOperation(
        op=op,
        path=path,
        value=value,
        authority=Authority.APPROVED_POLICY,
        reason="test",
    )


def _patch(*ops: PatchOperation, require_review: bool = False, reasons=()) -> BehaviorPatch:
    return BehaviorPatch(
        operations=tuple(ops),
        require_review=require_review,
        reasons=tuple(reasons),
    )


# ---------------------------------------------------------------------------
# Classification adapter
# ---------------------------------------------------------------------------

class TestClassificationAdapter:

    def test_build_context_has_correct_engine(self):
        ctx = build_classification_context("db.tbl", "col1", [])
        assert ctx.engine == "classification"
        assert ctx.facts["column.name"] == "col1"
        assert ctx.facts["candidates.count"] == 0

    def test_build_context_with_candidates(self):
        candidates = [
            {"tag": "PII.EMAIL", "score": 0.9},
            {"tag": "PII.PHONE", "score": 0.75},
        ]
        ctx = build_classification_context("db.tbl", "col1", candidates)
        assert ctx.facts["candidates.count"] == 2
        assert ctx.facts["candidates.max_score"] == pytest.approx(0.9)
        assert "PII.EMAIL" in ctx.facts["candidates.tags"]

    def test_add_tag_allowed(self):
        candidates = [{"tag": "PII.EMAIL", "score": 0.9}]
        patch = _patch(_op("candidate.add_tag", "PII.PHONE"))
        result = apply_patch_to_candidates(candidates, patch, policy_id="p1", rule_id="r1")
        assert isinstance(result, ClassificationPatchResult)
        assert "PII.PHONE" in result.added_tags
        assert any(c["tag"] == "PII.PHONE" for c in result.candidates)

    def test_drop_tag_removes_matching(self):
        candidates = [
            {"tag": "PII.EMAIL", "score": 0.9},
            {"tag": "PII.PHONE", "score": 0.75},
        ]
        patch = _patch(_op("candidate.drop_tag", "PII.EMAIL"))
        result = apply_patch_to_candidates(candidates, patch)
        assert "PII.EMAIL" in result.dropped_tags
        assert all(c["tag"] != "PII.EMAIL" for c in result.candidates)
        assert any(c["tag"] == "PII.PHONE" for c in result.candidates)

    def test_input_not_mutated(self):
        candidates = [{"tag": "PII.EMAIL", "score": 0.9}]
        original_len = len(candidates)
        patch = _patch(_op("candidate.add_tag", "PII.PHONE"))
        apply_patch_to_candidates(candidates, patch)
        assert len(candidates) == original_len

    def test_require_review_flag(self):
        patch = _patch(require_review=True)
        result = apply_patch_to_candidates([], patch)
        assert result.require_review is True

    def test_assert_not_forbidden_raises_for_forbidden(self):
        class FakePolicy:
            forbidden_tags = ["LAWFUL_INTERCEPT", "NATIONAL_SECURITY"]
        with pytest.raises(ValueError, match="forbidden"):
            assert_not_forbidden("LAWFUL_INTERCEPT", FakePolicy())

    def test_assert_not_forbidden_passes_safe_tag(self):
        class FakePolicy:
            forbidden_tags = ["LAWFUL_INTERCEPT"]
        assert_not_forbidden("PII.EMAIL", FakePolicy())  # should not raise

    def test_forbidden_tag_blocks_add(self):
        class FakePolicy:
            forbidden_tags = ["LAWFUL_INTERCEPT"]
        patch = _patch(_op("candidate.add_tag", "LAWFUL_INTERCEPT"))
        with pytest.raises(ValueError, match="forbidden"):
            apply_patch_to_candidates([], patch, policy=FakePolicy())

    def test_capabilities_post_evidence_stage(self):
        ctx = build_classification_context(
            "t", "c", [], stage=HookStage.POST_EVIDENCE
        )
        assert "classification.candidate.add_tag" in ctx.capabilities.allowed_actions

    def test_capabilities_post_action_stage_limited(self):
        ctx = build_classification_context(
            "t", "c", [], stage=HookStage.POST_ACTION
        )
        assert "classification.candidate.add_tag" not in ctx.capabilities.allowed_actions
        assert "core.review.require" in ctx.capabilities.allowed_actions

    def test_unknown_stage_empty_capabilities(self):
        ctx = build_classification_context(
            "t", "c", [], stage=HookStage.PRE_EVIDENCE
        )
        assert len(ctx.capabilities.allowed_actions) == 0

    def test_engine_enabled_respects_config(self):
        cfg_on = BehaviorConfig(enabled=True, mode="active")
        cfg_off = BehaviorConfig(enabled=False)
        assert classification_engine_enabled(cfg_on) is True
        assert classification_engine_enabled(cfg_off) is False

    def test_engine_shadow_mode_is_enabled(self):
        cfg = BehaviorConfig(enabled=True, mode="shadow")
        assert classification_engine_enabled(cfg) is True

    def test_engine_per_engine_override(self):
        cfg = BehaviorConfig(
            enabled=True,
            mode="disabled",
            engine_modes={"classification": "active"},
        )
        assert classification_engine_enabled(cfg) is True


# ---------------------------------------------------------------------------
# Quality adapter
# ---------------------------------------------------------------------------

class TestQualityAdapter:

    def test_build_context_correct_engine(self):
        ctx = build_quality_context("db.tbl", [])
        assert ctx.engine == "quality"

    def test_build_context_proposal_facts(self):
        proposals = [
            {"expectation_type": "expect_column_values_to_not_be_null",
             "column": "col1", "severity": "error"},
            {"expectation_type": "expect_column_values_to_be_unique",
             "column": "col2", "severity": "warning"},
        ]
        ctx = build_quality_context("t", proposals, column="col1")
        assert ctx.facts["proposals.count"] == 2
        assert ctx.facts["proposals.column_count"] == 1
        assert ctx.facts["proposals.error_count"] == 1

    def test_suppress_proposal(self):
        proposals = [
            {"expectation_type": "expect_column_values_to_not_be_null",
             "column": "col1", "severity": "error"},
        ]
        patch = _patch(_op("proposal.suppress"))
        result = apply_patch_to_proposals(
            proposals, patch,
            policy_id="p1", rule_id="r1",
            target_column="col1",
        )
        assert result[0]["status"] == "suppressed"
        assert result[0]["suppressed_by_policy"] == "p1"

    def test_suppress_does_not_delete_proposal(self):
        proposals = [{"expectation_type": "X", "column": "c1", "severity": "error"}]
        patch = _patch(_op("proposal.suppress"))
        result = apply_patch_to_proposals(proposals, patch)
        assert len(result) == 1
        assert result[0]["status"] == "suppressed"

    def test_change_severity(self):
        proposals = [{"expectation_type": "X", "column": "c1", "severity": "error"}]
        patch = _patch(_op("proposal.change_severity", "warning"))
        result = apply_patch_to_proposals(proposals, patch)
        assert result[0]["severity"] == "warning"

    def test_invalid_severity_rejected(self):
        proposals = [{"expectation_type": "X", "column": "c1", "severity": "error"}]
        patch = _patch(_op("proposal.change_severity", "critical"))  # not in allowed set
        result = apply_patch_to_proposals(proposals, patch)
        # Severity stays unchanged because "critical" is not valid
        assert result[0]["severity"] == "error"

    def test_column_filter_applies_only_to_target(self):
        proposals = [
            {"expectation_type": "A", "column": "c1", "severity": "error"},
            {"expectation_type": "B", "column": "c2", "severity": "error"},
        ]
        patch = _patch(_op("proposal.suppress"))
        result = apply_patch_to_proposals(proposals, patch, target_column="c1")
        assert result[0].get("status") == "suppressed"
        assert result[1].get("status") != "suppressed"

    def test_input_proposals_not_mutated(self):
        proposals = [{"expectation_type": "A", "column": "c1", "severity": "error"}]
        patch = _patch(_op("proposal.change_severity", "warning"))
        apply_patch_to_proposals(proposals, patch)
        assert proposals[0]["severity"] == "error"  # unchanged

    def test_require_review_sets_flag(self):
        proposals = [{"expectation_type": "A", "column": "c1", "severity": "error"}]
        patch = _patch(require_review=True)
        result = apply_patch_to_proposals(proposals, patch)
        assert result[0]["review_required"] is True

    def test_add_reason_accumulates(self):
        proposals = [{"expectation_type": "A", "column": "c1", "severity": "error"}]
        patch = _patch(_op("review.add_reason", "policy reason X"), reasons=("pre-reason",))
        result = apply_patch_to_proposals(proposals, patch)
        reasons = result[0]["behavior_reasons"]
        assert "pre-reason" in reasons
        assert "policy reason X" in reasons

    def test_engine_enabled(self):
        cfg = BehaviorConfig(enabled=True, mode="active")
        assert quality_engine_enabled(cfg) is True
        assert quality_engine_enabled(BehaviorConfig(enabled=False)) is False


# ---------------------------------------------------------------------------
# Profiling adapter
# ---------------------------------------------------------------------------

class TestProfilingAdapter:

    def test_build_context_correct_engine(self):
        ctx = build_profiling_context("db.tbl")
        assert ctx.engine == "profiling"

    def test_require_deep_profile_directive(self):
        patch = _patch(_op("profiling.require_deep_profile", True))
        directive = apply_patch_to_directive(patch)
        assert directive.require_deep_profile is True

    def test_select_valid_profiler(self):
        patch = _patch(_op("profiling.select_profiler", "duckdb"))
        directive = apply_patch_to_directive(patch)
        assert directive.selected_profiler == "duckdb"

    def test_select_invalid_profiler_rejected(self):
        patch = _patch(_op("profiling.select_profiler", "unknown_profiler_xyz"))
        directive = apply_patch_to_directive(patch)
        assert directive.selected_profiler is None

    def test_triage_threshold_within_bounds(self):
        patch = _patch(_op("profiling.triage_threshold", 0.65))
        directive = apply_patch_to_directive(patch)
        assert directive.triage_threshold == pytest.approx(0.65)

    def test_triage_threshold_clamped_to_max(self):
        patch = _patch(_op("profiling.triage_threshold", 1.5))
        directive = apply_patch_to_directive(patch)
        assert directive.triage_threshold == pytest.approx(1.0)

    def test_triage_threshold_clamped_to_min(self):
        patch = _patch(_op("profiling.triage_threshold", -0.5))
        directive = apply_patch_to_directive(patch)
        assert directive.triage_threshold == pytest.approx(0.0)

    def test_directive_is_dataclass(self):
        directive = ProfilingDirective()
        assert directive.require_deep_profile is False
        assert directive.selected_profiler is None
        assert directive.triage_threshold is None

    def test_allowed_profilers_set(self):
        assert "great_expectations" in _ALLOWED_PROFILERS
        assert "duckdb" in _ALLOWED_PROFILERS
        assert "openmetadata" in _ALLOWED_PROFILERS

    def test_capabilities_pre_verdict_stage(self):
        ctx = build_profiling_context("t", stage=HookStage.PRE_VERDICT)
        assert "profiling.require_deep_profile" in ctx.capabilities.allowed_actions
        assert "profiling.select_profiler" in ctx.capabilities.allowed_actions

    def test_capabilities_unknown_stage_empty(self):
        ctx = build_profiling_context("t", stage=HookStage.POST_ACTION)
        assert len(ctx.capabilities.allowed_actions) == 0

    def test_engine_enabled(self):
        cfg = BehaviorConfig(enabled=True, mode="active")
        assert profiling_engine_enabled(cfg) is True


# ---------------------------------------------------------------------------
# Masking adapter
# ---------------------------------------------------------------------------

class TestMaskingAdapter:

    def test_build_context_correct_engine(self):
        ctx = build_masking_context("db.tbl", "col1")
        assert ctx.engine == "masking"
        assert ctx.facts["column.name"] == "col1"

    def test_strategy_from_allowlist(self):
        rule = {"strategy": "hash", "params": {}}
        patch = _patch(_op("masking.strategy", "mask"))
        result = apply_patch_to_plan_rule(rule, patch)
        assert result["strategy"] == "mask"

    def test_invalid_strategy_rejected(self):
        rule = {"strategy": "hash", "params": {}}
        patch = _patch(_op("masking.strategy", "dangerous_exec"))
        result = apply_patch_to_plan_rule(rule, patch)
        assert result["strategy"] == "hash"  # unchanged

    def test_plan_params_merged(self):
        rule = {"strategy": "mask", "params": {"char": "*"}}
        patch = _patch(_op("masking.plan_params", {"length": 4}))
        result = apply_patch_to_plan_rule(rule, patch)
        assert result["params"]["char"] == "*"
        assert result["params"]["length"] == 4

    def test_key_param_rejected(self):
        rule = {"strategy": "encrypt"}
        patch = _patch(_op("masking.plan_params", {"key": "secret123"}))
        with pytest.raises(ValueError, match="key"):
            apply_patch_to_plan_rule(rule, patch)

    def test_seed_param_rejected(self):
        rule = {"strategy": "fpe"}
        patch = _patch(_op("masking.plan_params", {"seed": "myseed"}))
        with pytest.raises(ValueError, match="seed"):
            apply_patch_to_plan_rule(rule, patch)

    def test_require_approval_sets_review_required(self):
        rule = {}
        patch = _patch(_op("masking.require_approval"))
        result = apply_patch_to_plan_rule(rule, patch)
        assert result["review_required"] is True

    def test_input_rule_not_mutated(self):
        rule = {"strategy": "hash", "params": {}}
        patch = _patch(_op("masking.strategy", "mask"))
        apply_patch_to_plan_rule(rule, patch)
        assert rule["strategy"] == "hash"  # unchanged

    def test_build_directive_from_patch(self):
        patch = _patch(
            _op("masking.strategy", "fpe"),
            _op("masking.plan_params", {"start_index": 2}),
        )
        directive = build_directive_from_patch(patch)
        assert isinstance(directive, MaskingDirective)
        assert directive.suggested_strategy == "fpe"
        assert directive.plan_params["start_index"] == 2

    def test_directive_no_key_in_params(self):
        patch = _patch(_op("masking.plan_params", {"key": "secret"}))
        with pytest.raises(ValueError):
            build_directive_from_patch(patch)

    def test_allowed_strategies_coverage(self):
        expected_subset = {"mask", "hash", "encrypt", "fpe", "fake"}
        assert expected_subset <= ALLOWED_MASKING_STRATEGIES

    def test_engine_enabled(self):
        cfg = BehaviorConfig(enabled=True, mode="active")
        assert masking_engine_enabled(cfg) is True
        assert masking_engine_enabled(BehaviorConfig(enabled=False)) is False

    def test_capabilities_pre_action_stage(self):
        ctx = build_masking_context("t", "c", stage=HookStage.PRE_ACTION)
        assert "masking.suggest_strategy" in ctx.capabilities.allowed_actions
        assert "masking.require_approval" in ctx.capabilities.allowed_actions

    def test_capabilities_unknown_stage_empty(self):
        ctx = build_masking_context("t", "c", stage=HookStage.PRE_EVIDENCE)
        assert len(ctx.capabilities.allowed_actions) == 0
