"""Tests for IntentPlanner (Phase 2)."""

from __future__ import annotations

import json

import pytest

from redibis.agents.models import PipelineSpec
from redibis.agents.planner import IntentPlanner, PlannerContext, heuristic_plan
from redibis.agents.registry import validate_spec


def _valid_plan_json() -> str:
    return json.dumps({
        "name": "telco_onboard",
        "goal": "Onboard telco tables, classify, contract, publish",
        "nodes": [
            {"kind": "source", "label": "Hive", "params": {"engine": "hive", "table": "telco_cdw"}},
            {"kind": "sample", "label": "Sample", "params": {"strategy": "percent", "amount": 10}},
            {"kind": "profile", "label": "Profile", "params": {}},
            {"kind": "contract", "label": "Contract", "params": {"pii": True, "classify": True, "quality": True}},
            {"kind": "mask", "label": "Mask", "params": {}},
            {"kind": "gate", "label": "Gate", "params": {"role": "Steward"}},
            {"kind": "publish", "label": "OM", "params": {"backend": "openmetadata"}},
        ],
        "edges": [
            {"source": 0, "target": 1},
            {"source": 1, "target": 2},
            {"source": 1, "target": 3},
            {"source": 2, "target": 3},
            {"source": 3, "target": 4},
            {"source": 3, "target": 5},
            {"source": 5, "target": 6},
        ],
    })


def test_intent_planner_produces_valid_spec():
    def fake_call(prompt: str):
        return _valid_plan_json(), None

    planner = IntentPlanner(model_call=fake_call)
    result = planner.plan(
        "onboard all telco_cdw, classify, contract, recommend masking, publish to OM",
        PlannerContext(policy_pack="telecom", database="telco_cdw"),
    )
    assert result.valid
    assert validate_spec(result.spec) == []
    kinds = {n.kind for n in result.spec.nodes}
    assert {"source", "sample", "profile", "contract", "mask", "gate", "publish"} <= kinds


def test_intent_planner_rejects_invalid_then_repairs():
    calls = {"n": 0}

    def fake_call(prompt: str):
        calls["n"] += 1
        if calls["n"] == 1:
            # missing sample→contract data edge
            bad = json.loads(_valid_plan_json())
            bad["edges"] = [
                {"source": 0, "target": 1},
                {"source": 1, "target": 2},
                {"source": 2, "target": 3},
                {"source": 3, "target": 5},
                {"source": 5, "target": 6},
            ]
            return json.dumps(bad), None
        return _valid_plan_json(), None

    planner = IntentPlanner(model_call=fake_call)
    result = planner.plan("onboard telco")
    assert result.valid
    assert result.repaired


def test_intent_planner_flags_unrecoverable_invalid():
    def bad_call(prompt: str):
        return json.dumps({"name": "x", "nodes": [], "edges": []}), None

    planner = IntentPlanner(model_call=bad_call)
    result = planner.plan("do something")
    assert not result.valid
    assert result.errors


def test_intent_planner_requires_provider_or_model_call():
    planner = IntentPlanner()
    with pytest.raises(ValueError, match="provider or model_call"):
        planner.plan("onboard tables")


def test_heuristic_plan_onboard_publish_valid():
    result = heuristic_plan(
        "plan onboard telecom.customers — classify, PII, quality, publish to OM",
        PlannerContext(policy_pack="telecom"),
    )
    assert result.valid
    assert validate_spec(result.spec) == []
    kinds = [n.kind for n in result.spec.nodes]
    assert kinds == ["source", "sample", "profile", "contract", "gate", "publish"]
    assert result.spec.nodes[0].params.get("table") == "telecom.customers"


def test_heuristic_plan_mask_includes_mask_node():
    result = heuristic_plan("profile and mask telco_cdw")
    assert result.valid
    kinds = {n.kind for n in result.spec.nodes}
    assert "mask" in kinds
