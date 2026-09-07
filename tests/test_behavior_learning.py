"""
Phase 9 learning loop tests.

Tests for: outcome labels, rule signals, suggestions, no auto-disable,
draft from corrections.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from redibis.behavior.learning import (
    OutcomeLabel,
    RuleSignal,
    Suggestion,
    compute_rule_signals,
    draft_from_repeated_corrections,
    load_outcome_labels,
    make_context_hash,
    record_outcome_label,
    suggest_narrow_or_retire,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _label(
    policy_id="policy_a",
    rule_id="rule_1",
    human_result="confirmed",
    table="db.tbl",
    column="col1",
    engine="pii",
) -> OutcomeLabel:
    return OutcomeLabel(
        policy_id=policy_id,
        rule_id=rule_id,
        policy_sha256="abc123",
        original_verdict="pii_detected",
        policy_verdict="not_pii",
        human_result=human_result,
        context_hash=make_context_hash({"column.name": column, "verdict.detected": True}),
        engine=engine,
        table=table,
        column=column,
    )


# ---------------------------------------------------------------------------
# OutcomeLabel model
# ---------------------------------------------------------------------------

class TestOutcomeLabel:

    def test_valid_label_confirmed(self):
        lbl = _label(human_result="confirmed")
        assert lbl.human_result == "confirmed"

    def test_valid_label_reversed(self):
        lbl = _label(human_result="reversed")
        assert lbl.human_result == "reversed"

    def test_valid_label_unknown(self):
        lbl = _label(human_result="unknown")
        assert lbl.human_result == "unknown"

    def test_invalid_human_result_raises(self):
        with pytest.raises(ValueError, match="human_result"):
            OutcomeLabel(
                policy_id="p",
                rule_id="r",
                policy_sha256="sha",
                original_verdict="v1",
                policy_verdict="v2",
                human_result="approved",  # invalid
                context_hash="hash",
                engine="pii",
                table="t",
            )

    def test_empty_policy_id_raises(self):
        with pytest.raises(ValueError):
            OutcomeLabel(
                policy_id="",
                rule_id="r1",
                policy_sha256="sha",
                original_verdict="v1",
                policy_verdict="v2",
                human_result="confirmed",
                context_hash="hash",
                engine="pii",
                table="t",
            )

    def test_empty_context_hash_raises(self):
        with pytest.raises(ValueError):
            OutcomeLabel(
                policy_id="p",
                rule_id="r",
                policy_sha256="sha",
                original_verdict="v1",
                policy_verdict="v2",
                human_result="confirmed",
                context_hash="",
                engine="pii",
                table="t",
            )

    def test_label_has_timestamp(self):
        lbl = _label()
        assert lbl.ts  # non-empty ISO timestamp


# ---------------------------------------------------------------------------
# make_context_hash
# ---------------------------------------------------------------------------

class TestMakeContextHash:

    def test_deterministic(self):
        facts = {"column.name": "col1", "verdict.detected": True, "verdict.confidence": 0.9}
        h1 = make_context_hash(facts)
        h2 = make_context_hash(facts)
        assert h1 == h2

    def test_different_facts_different_hash(self):
        h1 = make_context_hash({"column.name": "col1"})
        h2 = make_context_hash({"column.name": "col2"})
        assert h1 != h2

    def test_hash_is_hex_string(self):
        h = make_context_hash({"x": 1})
        assert len(h) == 64
        int(h, 16)  # should parse as hex


# ---------------------------------------------------------------------------
# record_outcome_label and load
# ---------------------------------------------------------------------------

class TestRecordAndLoad:

    def test_record_creates_file(self):
        with tempfile.TemporaryDirectory() as base:
            lbl = _label()
            path = record_outcome_label(base, lbl)
            assert path.exists()
            content = path.read_text()
            assert '"policy_id"' in content

    def test_record_appends(self):
        with tempfile.TemporaryDirectory() as base:
            lbl1 = _label(human_result="confirmed")
            lbl2 = _label(human_result="reversed")
            record_outcome_label(base, lbl1)
            record_outcome_label(base, lbl2)
            path = Path(base) / "outcomes" / "policy_a" / "rule_1.jsonl"
            lines = [l for l in path.read_text().splitlines() if l.strip()]
            assert len(lines) == 2

    def test_load_all_returns_labels(self):
        with tempfile.TemporaryDirectory() as base:
            record_outcome_label(base, _label(human_result="confirmed"))
            record_outcome_label(base, _label(human_result="reversed"))
            labels = load_outcome_labels(base)
            assert len(labels) == 2

    def test_load_with_policy_filter(self):
        with tempfile.TemporaryDirectory() as base:
            record_outcome_label(base, _label(policy_id="policy_a"))
            record_outcome_label(base, _label(policy_id="policy_b"))
            labels = load_outcome_labels(base, policy_id="policy_a")
            assert len(labels) == 1
            assert labels[0].policy_id == "policy_a"

    def test_load_empty_dir_returns_empty(self):
        with tempfile.TemporaryDirectory() as base:
            labels = load_outcome_labels(base)
            assert labels == []

    def test_round_trip_preserves_fields(self):
        with tempfile.TemporaryDirectory() as base:
            lbl = _label(human_result="reversed", column="phone")
            record_outcome_label(base, lbl)
            loaded = load_outcome_labels(base)
            assert len(loaded) == 1
            assert loaded[0].human_result == "reversed"
            assert loaded[0].column == "phone"
            assert loaded[0].policy_id == "policy_a"


# ---------------------------------------------------------------------------
# compute_rule_signals
# ---------------------------------------------------------------------------

class TestComputeRuleSignals:

    def test_signals_from_mixed_outcomes(self):
        with tempfile.TemporaryDirectory() as base:
            for _ in range(3):
                record_outcome_label(base, _label(human_result="confirmed"))
            for _ in range(2):
                record_outcome_label(base, _label(human_result="reversed"))
            for _ in range(1):
                record_outcome_label(base, _label(human_result="unknown"))

            signals = compute_rule_signals(base)
            assert len(signals) == 1
            s = signals[0]
            assert s.applications == 6
            assert s.confirmations == 3
            assert s.reversals == 2
            assert s.unknown_count == 1
            assert s.unknown_conflict_rate == pytest.approx(1 / 6)

    def test_estimated_precision_delta_bounds(self):
        with tempfile.TemporaryDirectory() as base:
            for _ in range(5):
                record_outcome_label(base, _label(human_result="reversed"))
            signals = compute_rule_signals(base)
            s = signals[0]
            assert -1.0 <= s.estimated_precision_delta <= 1.0
            assert s.estimated_precision_delta < 0  # all reversed

    def test_all_confirmed_positive_delta(self):
        with tempfile.TemporaryDirectory() as base:
            for _ in range(5):
                record_outcome_label(base, _label(human_result="confirmed"))
            signals = compute_rule_signals(base)
            s = signals[0]
            assert s.estimated_precision_delta == pytest.approx(1.0)

    def test_domain_distribution(self):
        with tempfile.TemporaryDirectory() as base:
            record_outcome_label(base, _label(table="db.tbl_a"))
            record_outcome_label(base, _label(table="db.tbl_b"))
            record_outcome_label(base, _label(table="db.tbl_a"))
            signals = compute_rule_signals(base)
            dist = signals[0].domain_distribution
            assert dist["db.tbl_a"] == 2
            assert dist["db.tbl_b"] == 1

    def test_multiple_rules_separate_signals(self):
        with tempfile.TemporaryDirectory() as base:
            record_outcome_label(base, _label(rule_id="rule_1"))
            record_outcome_label(base, _label(rule_id="rule_2"))
            signals = compute_rule_signals(base)
            assert len(signals) == 2

    def test_policy_filter(self):
        with tempfile.TemporaryDirectory() as base:
            record_outcome_label(base, _label(policy_id="p_a"))
            record_outcome_label(base, _label(policy_id="p_b"))
            signals = compute_rule_signals(base, policy_id="p_a")
            assert len(signals) == 1
            assert signals[0].policy_id == "p_a"

    def test_empty_store_returns_empty(self):
        with tempfile.TemporaryDirectory() as base:
            signals = compute_rule_signals(base)
            assert signals == []

    def test_field_names_are_not_precision_recall(self):
        # Ensure we don't use misleading names like "precision" or "recall"
        sig = RuleSignal(
            policy_id="p",
            rule_id="r",
            policy_sha256="sha",
            engine="pii",
            applications=5,
            confirmations=4,
            reversals=1,
            unknown_count=0,
            unknown_conflict_rate=0.0,
            estimated_precision_delta=0.6,
            domain_distribution={},
        )
        # Verify the field name is "estimated_precision_delta" not "precision"
        assert hasattr(sig, "estimated_precision_delta")
        assert not hasattr(sig, "precision")
        assert not hasattr(sig, "recall")


# ---------------------------------------------------------------------------
# suggest_narrow_or_retire — no auto-disable
# ---------------------------------------------------------------------------

class TestSuggestNarrowOrRetire:

    def _signal(
        self,
        applications=10,
        confirmations=2,
        reversals=5,
        unknown_count=3,
        tables=None,
    ) -> RuleSignal:
        domain = {t: 1 for t in (tables or ["tbl"])}
        total = applications
        unknown_rate = unknown_count / total if total else 0.0
        delta = (confirmations - reversals) / total if total else 0.0
        return RuleSignal(
            policy_id="policy_a",
            rule_id="rule_1",
            policy_sha256="sha",
            engine="pii",
            applications=applications,
            confirmations=confirmations,
            reversals=reversals,
            unknown_count=unknown_count,
            unknown_conflict_rate=unknown_rate,
            estimated_precision_delta=delta,
            domain_distribution=domain,
        )

    def test_high_reversal_produces_suggestion(self):
        sig = self._signal(applications=10, confirmations=1, reversals=6, unknown_count=3)
        suggestions = suggest_narrow_or_retire([sig])
        assert len(suggestions) >= 1
        assert any(s.kind in ("consider_retirement", "review_needed") for s in suggestions)

    def test_high_reversal_extreme_consider_retirement(self):
        sig = self._signal(applications=10, confirmations=0, reversals=8, unknown_count=2)
        suggestions = suggest_narrow_or_retire([sig])
        kinds = [s.kind for s in suggestions]
        assert "consider_retirement" in kinds

    def test_low_applications_no_signal(self):
        sig = self._signal(applications=3, confirmations=0, reversals=2, unknown_count=1)
        suggestions = suggest_narrow_or_retire([sig])
        # Fewer than _MIN_APPLICATIONS_FOR_SIGNAL (5), so no suggestion
        assert len(suggestions) == 0

    def test_high_unknown_rate_review_needed(self):
        sig = RuleSignal(
            policy_id="p", rule_id="r", policy_sha256="sha",
            engine="quality",
            applications=10,
            confirmations=2,
            reversals=1,
            unknown_count=7,
            unknown_conflict_rate=0.7,
            estimated_precision_delta=0.1,
            domain_distribution={"t": 10},
        )
        suggestions = suggest_narrow_or_retire([sig])
        assert any(s.kind == "review_needed" for s in suggestions)

    def test_broad_scope_narrow_suggestion(self):
        tables = [f"tbl_{i}" for i in range(8)]
        sig = RuleSignal(
            policy_id="p", rule_id="r", policy_sha256="sha",
            engine="pii",
            applications=10,
            confirmations=8,
            reversals=2,
            unknown_count=0,
            unknown_conflict_rate=0.0,
            estimated_precision_delta=0.6,
            domain_distribution={t: 1 for t in tables},
        )
        suggestions = suggest_narrow_or_retire([sig])
        assert any(s.kind == "narrow_scope" for s in suggestions)

    def test_suggestions_never_auto_disable(self):
        """CRITICAL: no suggestion has a kind that implies auto-disable."""
        auto_disable_kinds = {"disabled", "auto_disabled", "auto_rewrite", "deactivate"}
        sig = self._signal(applications=10, confirmations=0, reversals=10, unknown_count=0)
        suggestions = suggest_narrow_or_retire([sig])
        for s in suggestions:
            assert s.kind not in auto_disable_kinds, (
                f"suggest_narrow_or_retire returned auto-disable kind: {s.kind}"
            )

    def test_suggestions_have_supporting_signals(self):
        sig = self._signal(applications=10, confirmations=1, reversals=6, unknown_count=3)
        suggestions = suggest_narrow_or_retire([sig])
        if suggestions:
            assert isinstance(suggestions[0].supporting_signals, dict)
            assert len(suggestions[0].supporting_signals) > 0

    def test_good_rule_no_suggestion(self):
        sig = RuleSignal(
            policy_id="p", rule_id="r", policy_sha256="sha",
            engine="pii",
            applications=20,
            confirmations=18,
            reversals=1,
            unknown_count=1,
            unknown_conflict_rate=0.05,
            estimated_precision_delta=0.85,
            domain_distribution={"tbl": 20},
        )
        suggestions = suggest_narrow_or_retire([sig])
        assert suggestions == []


# ---------------------------------------------------------------------------
# draft_from_repeated_corrections — no auto-activate
# ---------------------------------------------------------------------------

class TestDraftFromRepeatedCorrections:

    def test_no_corrections_returns_none(self):
        result = draft_from_repeated_corrections([], engine="pii")
        assert result is None

    def test_below_min_repetitions_returns_none(self):
        corrections = [
            {"column": "col1", "original_verdict": "pii", "desired_verdict": False, "reason": "r"},
            {"column": "col1", "original_verdict": "pii", "desired_verdict": False, "reason": "r"},
        ]
        result = draft_from_repeated_corrections(corrections, engine="pii", min_repetitions=3)
        assert result is None

    def test_qualifying_pattern_produces_draft(self):
        corrections = [
            {"column": "col1", "original_verdict": "pii_detected", "desired_verdict": False,
             "reason": "false positive", "table": "t"}
            for _ in range(5)
        ]
        result = draft_from_repeated_corrections(corrections, engine="pii", min_repetitions=3)
        assert result is not None
        assert isinstance(result, dict)

    def test_draft_has_requires_human_approval(self):
        corrections = [
            {"column": "col1", "original_verdict": "pii", "desired_verdict": False, "reason": "r"}
            for _ in range(5)
        ]
        draft = draft_from_repeated_corrections(corrections, engine="pii", min_repetitions=3)
        assert draft is not None
        lifecycle = draft.get("lifecycle") or {}
        assert lifecycle.get("requires_human_approval") is True

    def test_draft_has_draft_status(self):
        corrections = [
            {"column": "col1", "original_verdict": "pii", "desired_verdict": False, "reason": "r"}
            for _ in range(5)
        ]
        draft = draft_from_repeated_corrections(corrections, engine="pii", min_repetitions=3)
        assert draft is not None
        lifecycle = draft.get("lifecycle") or {}
        assert lifecycle.get("status") == "draft"

    def test_draft_source_is_llm_proposed(self):
        corrections = [
            {"column": "col1", "original_verdict": "pii", "desired_verdict": False, "reason": "r"}
            for _ in range(5)
        ]
        draft = draft_from_repeated_corrections(corrections, engine="pii", min_repetitions=3)
        assert draft is not None
        assert draft.get("source") == "llm_proposed"

    def test_draft_contains_source_stats(self):
        corrections = [
            {"column": "col1", "original_verdict": "pii", "desired_verdict": False, "reason": "r"}
            for _ in range(5)
        ]
        draft = draft_from_repeated_corrections(corrections, engine="pii", min_repetitions=3)
        assert draft is not None
        stats = (draft.get("lifecycle") or {}).get("source_stats") or {}
        assert stats.get("total_corrections") == 5

    def test_draft_never_activated(self):
        """CRITICAL: draft must not call activate or set status != 'draft'."""
        corrections = [
            {"column": "col1", "original_verdict": "pii", "desired_verdict": False, "reason": "r"}
            for _ in range(5)
        ]
        draft = draft_from_repeated_corrections(corrections, engine="pii", min_repetitions=3)
        assert draft is not None
        lifecycle = draft.get("lifecycle") or {}
        # Must NOT be "active"
        assert lifecycle.get("status") != "active"
        # Must NOT have approved=True
        assert lifecycle.get("approved") is not True

    def test_narrow_scope_to_observed_columns(self):
        corrections = [
            {"column": "col1", "original_verdict": "pii", "desired_verdict": False, "reason": "r"}
            for _ in range(5)
        ]
        draft = draft_from_repeated_corrections(corrections, engine="pii", min_repetitions=3)
        assert draft is not None
        scope = draft.get("scope") or {}
        assert "col1" in (scope.get("columns") or [])

    def test_multiple_patterns_multiple_rules(self):
        corrections = []
        for _ in range(4):
            corrections.append({
                "column": "col1", "original_verdict": "pii",
                "desired_verdict": False, "reason": "r"
            })
        for _ in range(4):
            corrections.append({
                "column": "col2", "original_verdict": "pii",
                "desired_verdict": False, "reason": "r"
            })
        draft = draft_from_repeated_corrections(corrections, engine="pii", min_repetitions=3)
        assert draft is not None
        assert len(draft.get("rules") or []) >= 2
