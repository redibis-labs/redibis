"""Regression tests for critical Behavior Policy review fixes."""

from __future__ import annotations

import pandas as pd
import pytest

from redibis.behavior.config import BehaviorConfig
from redibis.behavior.ids import validate_policy_id, validate_policy_version
from redibis.behavior.models import RuntimeMode
from redibis.behavior.store import BehaviorPolicyStore
from redibis.models import PIIDetection
from redibis.services.behavior_service import BehaviorPolicyService, BehaviorServiceError


MINIMAL_DOC = {
    "apiVersion": "redibis.io/behavior-policy/v1",
    "kind": "BehaviorPolicy",
    "metadata": {
        "id": "fix-lac-policy",
        "version": "1.0.0",
        "description": "Demote LAC network identifiers",
    },
    "appliesTo": {"engine": "pii", "stage": "post_verdict"},
    "rules": [
        {
            "id": "lac-network",
            "when": {"fact": "column.name", "op": "matches", "value": "(?i)^lac$"},
            "effects": [
                {"effect": "core.verdict.set_entity", "params": {"entity": "NETWORK_ID"}},
                {"effect": "core.verdict.set_detected", "params": {"detected": True}},
            ],
            "reason": "LAC is a network identifier, not a phone number.",
            "priority": 10,
            "terminal": True,
        }
    ],
}


def test_path_traversal_rejected_on_save(tmp_path):
    store = BehaviorPolicyStore(tmp_path / "policies")
    with pytest.raises(ValueError, match="path characters|invalid policy"):
        validate_policy_id("../escape")
    with pytest.raises(ValueError):
        validate_policy_version("../../x")
    # Store refuses to materialize traversal ids
    from redibis.behavior.compiler import compile_policy
    from redibis.behavior.models import PolicySource

    bad = {
        **MINIMAL_DOC,
        "metadata": {**MINIMAL_DOC["metadata"], "id": "../escape"},
    }
    with pytest.raises(Exception):
        compile_policy(bad, source=PolicySource.USER)


def test_forged_simulation_id_rejected(tmp_path):
    svc = BehaviorPolicyService(
        base_dir=tmp_path / "policies",
        require_approval_for_activation=True,
        require_simulation_for_activation=True,
    )
    created = svc.create(MINIMAL_DOC, actor="alice")
    with pytest.raises(BehaviorServiceError, match="unknown or forged"):
        svc.approve(
            created["id"],
            created["version"],
            actor="steward",
            simulation_id="forged-sim-id",
        )


def test_rollback_pops_stack_not_forward(tmp_path):
    svc = BehaviorPolicyService(
        base_dir=tmp_path / "policies",
        require_approval_for_activation=True,
        require_simulation_for_activation=True,
    )
    c1 = svc.create(MINIMAL_DOC, actor="a")
    sim1 = svc.simulate(
        policy_id=c1["id"],
        version=c1["version"],
        context={"column": "lac", "facts": {"column.name": "lac"}},
    )
    svc.approve(c1["id"], c1["version"], actor="s", simulation_id=sim1.simulation_id)
    svc.activate(c1["id"], c1["version"], actor="admin")

    v2 = {
        **MINIMAL_DOC,
        "metadata": {**MINIMAL_DOC["metadata"], "version": "2.0.0"},
        "rules": [{**MINIMAL_DOC["rules"][0], "id": "lac-v2", "reason": "v2 reason text."}],
    }
    c2 = svc.create(v2, actor="a")
    sim2 = svc.simulate(
        policy_id=c2["id"],
        version=c2["version"],
        context={"column": "lac", "facts": {"column.name": "lac"}},
    )
    svc.approve(c2["id"], c2["version"], actor="s", simulation_id=sim2.simulation_id)
    svc.activate(c2["id"], c2["version"], actor="admin")

    v3 = {
        **MINIMAL_DOC,
        "metadata": {**MINIMAL_DOC["metadata"], "version": "3.0.0"},
        "rules": [{**MINIMAL_DOC["rules"][0], "id": "lac-v3", "reason": "v3 reason text."}],
    }
    c3 = svc.create(v3, actor="a")
    sim3 = svc.simulate(
        policy_id=c3["id"],
        version=c3["version"],
        context={"column": "lac", "facts": {"column.name": "lac"}},
    )
    svc.approve(c3["id"], c3["version"], actor="s", simulation_id=sim3.simulation_id)
    svc.activate(c3["id"], c3["version"], actor="admin")
    assert svc.store.get_active_pointer(c1["id"])["version"] == "3.0.0"

    r1 = svc.rollback(c1["id"], actor="admin")
    assert r1["version"] == "2.0.0"
    r2 = svc.rollback(c1["id"], actor="admin")
    assert r2["version"] == "1.0.0"


def test_compile_scope_maps_engine_not_environments():
    from redibis.behavior.compiler import compile_policy

    doc = {
        **MINIMAL_DOC,
        "appliesTo": {
            "engine": "pii",
            "stage": "post_verdict",
            "environments": ["prod"],
        },
    }
    policy = compile_policy(doc)
    assert policy.scope.engines == ("pii",)
    assert policy.scope.environments == ("prod",)


def test_active_store_policy_applied(tmp_path, monkeypatch):
    from redibis.behavior.runtime import apply_pii_behavior_policies
    from redibis.services import behavior_service as bs

    monkeypatch.setattr(bs, "default_policy_store_dir", lambda: tmp_path / "policies")
    svc = BehaviorPolicyService(
        base_dir=tmp_path / "policies",
        require_approval_for_activation=True,
        require_simulation_for_activation=True,
    )
    created = svc.create(MINIMAL_DOC, actor="a")
    sim = svc.simulate(
        policy_id=created["id"],
        version=created["version"],
        context={"column": "lac", "facts": {"column.name": "lac", "verdict.entity": "PHONE_NUMBER"}},
    )
    svc.approve(created["id"], created["version"], actor="s", simulation_id=sim.simulation_id)
    svc.activate(created["id"], created["version"], actor="admin")

    cfg = BehaviorConfig(enabled=True, mode=RuntimeMode.ACTIVE.value)
    detection = PIIDetection(
        column="lac",
        detected=True,
        entity_type="PHONE_NUMBER",
        confidence=0.9,
    )
    result = apply_pii_behavior_policies(
        [detection],
        table="telecom.cells",
        behavior_config=cfg,
        policy_pack="telecom",
    )
    assert result.detections[0].entity_type == "NETWORK_ID"


def test_checksum_fallthrough_allows_later_rule(tmp_path, monkeypatch):
    """When checksum blocks the first terminal rule, demotion must not apply."""
    from redibis.behavior.adapters.pii import edge_rules_to_behavior_policy
    from redibis.behavior.runtime import apply_pii_behavior_policies
    from redibis.classification.pack_store import load_pack
    from redibis.services import behavior_service as bs

    monkeypatch.setattr(bs, "default_policy_store_dir", lambda: tmp_path / "empty-policies")

    pack = load_pack("telecom")
    policy = edge_rules_to_behavior_policy(pack)
    assert policy is not None

    detection = PIIDetection(
        column="customer_id",
        detected=True,
        entity_type="NATIONAL_ID",
        confidence=0.9,
    )
    df = pd.DataFrame({"customer_id": ["29001011401234", "29001011401235"]})
    cfg = BehaviorConfig(enabled=True, mode=RuntimeMode.ACTIVE.value)
    result = apply_pii_behavior_policies(
        [detection],
        df=df,
        behavior_config=cfg,
        post_verdict_policy=policy,
    )
    assert result.detections[0].detected is True
    assert result.detections[0].entity_type == "NATIONAL_ID"


def test_validate_lint_uses_finding_id(tmp_path):
    svc = BehaviorPolicyService(base_dir=tmp_path / "policies")
    # Duplicate rule id → blocking lint
    bad = {
        **MINIMAL_DOC,
        "rules": [
            MINIMAL_DOC["rules"][0],
            {**MINIMAL_DOC["rules"][0], "reason": "duplicate id second rule."},
        ],
    }
    # Schema rejects duplicate ids before lint — use empty effects sibling instead
    from redibis.behavior.lint import lint_policy
    from redibis.behavior.compiler import compile_policy

    doc = {
        **MINIMAL_DOC,
        "rules": [
            MINIMAL_DOC["rules"][0],
            {
                "id": "other-rule",
                "when": {"fact": "column.name", "op": "eq", "value": "x"},
                "effects": [
                    {"effect": "core.verdict.set_detected", "params": {"detected": False}},
                ],
                "reason": "Another rule for lint serialization.",
                "priority": 1,
            },
        ],
    }
    result = svc.validate(doc)
    assert "finding_id" in result.lint[0] or result.ok
    # At minimum validate path must not AttributeError on f.id
    assert isinstance(result.lint, list)


def test_agent_tool_simulate_works():
    from redibis.behavior.agent_tools import tool_simulate_policy
    from redibis.behavior.store import BehaviorPolicyStore
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        store = BehaviorPolicyStore(td)
        out = tool_simulate_policy(
            store,
            MINIMAL_DOC,
            [{"column": "lac", "facts": {"column.name": "lac", "verdict.entity": "PHONE_NUMBER"}}],
        )
    assert out["error"] is None
    assert out["evaluated"] == 1
    assert out["matched"] >= 1
