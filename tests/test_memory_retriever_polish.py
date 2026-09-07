"""Tests for memory retriever conflict surfacing and contract NDV cardinality."""

from __future__ import annotations

from redibis.memory.decision import ReviewDecision
from redibis.memory.retriever import RetrievedContext, _decision_conflicts, hint_to_dict
from redibis.memory.writer import fingerprint_from_column_prop


def test_decision_conflicts_detects_pii_disagreement():
    decisions = [
        ReviewDecision("", "t", "email", pii_verdict="pii"),
        ReviewDecision("", "t", "email", pii_verdict="not_pii"),
    ]
    conflicts = _decision_conflicts(decisions)
    assert any("PII verdict" in c for c in conflicts)


def test_hint_to_dict_includes_conflicts():
    ctx = RetrievedContext(
        fingerprint_summary={"name_normalized": "email"},
        decisions=[
            ReviewDecision("", "t", "email", pii_verdict="pii"),
            ReviewDecision("", "t", "email", pii_verdict="not_pii"),
        ],
        occurrence_count=3,
    )
    hint = hint_to_dict(ctx)
    assert hint["occurrence_count"] == 3
    assert hint["prior_decisions"] == 2
    assert hint["conflicts"]


def test_fingerprint_from_prop_uses_catalog_ndv():
    prop = {
        "name": "status",
        "logicalType": "string",
        "customProperties": {"ndv": 5, "row_count": 100},
    }
    fp = fingerprint_from_column_prop("db.t", "status", prop)
    assert fp.cardinality_class.source == "catalog"
    assert fp.cardinality_class.value == "categorical"
