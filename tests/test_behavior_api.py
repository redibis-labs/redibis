"""Phase 5 Behavior Policy API + service tests."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from redibis.services.behavior_service import BehaviorPolicyService, BehaviorServiceError


MINIMAL_DOC = {
    "apiVersion": "redibis.io/behavior-policy/v1",
    "kind": "BehaviorPolicy",
    "metadata": {
        "id": "test-lac-policy",
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


@pytest.fixture
def svc(tmp_path):
    return BehaviorPolicyService(
        base_dir=tmp_path / "policies",
        require_approval_for_activation=True,
        require_simulation_for_activation=True,
    )


@pytest.fixture
def client(tmp_path, monkeypatch):
    from redibis.webapp import behavior_routes
    from redibis.webapp.backend import app

    store_dir = tmp_path / "api-policies"

    def _factory():
        return BehaviorPolicyService(
            base_dir=store_dir,
            require_approval_for_activation=True,
            require_simulation_for_activation=True,
        )

    # Re-register with tmp store
    behavior_routes.register_behavior_routes(app, service_factory=_factory)
    return TestClient(app)


def test_catalog_deterministic(svc):
    a = svc.catalog()
    b = svc.catalog()
    assert a["manifest_sha256"] == b["manifest_sha256"]
    assert "facts" in a["catalogue"]
    assert "effects" in a["catalogue"]
    assert "core.verdict.set_entity" in a["catalogue"]["effects"]


def test_validate_rejects_unknown_effect(svc):
    bad = {
        **MINIMAL_DOC,
        "rules": [
            {
                "id": "bad",
                "when": {"fact": "column.name", "op": "eq", "value": "x"},
                "effects": [{"effect": "core.not.a.real.effect", "params": {}}],
                "reason": "Should fail registry check.",
            }
        ],
    }
    result = svc.validate(bad)
    assert result.ok is False
    assert any("unknown effect" in e for e in result.errors)


def test_validate_ok_and_order(svc):
    result = svc.validate(MINIMAL_DOC)
    assert result.ok is True
    assert result.eligible_for_activation is True
    assert result.effective_rule_order == ["lac-network"]
    assert len(result.content_sha256) == 64


def test_create_immutable_sha_conflict(svc):
    svc.create(MINIMAL_DOC, actor="alice")
    changed = {
        **MINIMAL_DOC,
        "rules": [
            {
                **MINIMAL_DOC["rules"][0],
                "reason": "Different reason changes SHA.",
            }
        ],
    }
    with pytest.raises(BehaviorServiceError, match="different SHA"):
        svc.create(changed, actor="alice")


def test_simulate_write_free_and_match(svc):
    created = svc.create(MINIMAL_DOC, actor="alice")
    result = svc.simulate(
        policy_id=created["id"],
        version=created["version"],
        context={
            "column": "lac",
            "table": "telecom.cells",
            "facts": {
                "column.name": "lac",
                "verdict.detected": True,
                "verdict.entity": "PHONE_NUMBER",
                "verdict.confidence": 0.9,
            },
        },
    )
    assert result.simulation_id
    assert result.aggregate["verdict_changes"] >= 1
    assert result.after.get("entity") == "NETWORK_ID"
    # No contract writes — store only has policy files
    assert (svc.store._base / "test-lac-policy" / "1.0.0.json").exists()


def test_approve_activate_deactivate_rollback(svc):
    created = svc.create(MINIMAL_DOC, actor="alice")
    pid, ver = created["id"], created["version"]
    sim = svc.simulate(
        policy_id=pid,
        version=ver,
        context={
            "column": "lac",
            "facts": {
                "column.name": "lac",
                "verdict.entity": "PHONE_NUMBER",
                "verdict.detected": True,
            },
        },
    )
    with pytest.raises(BehaviorServiceError, match="not approved"):
        svc.activate(pid, ver, actor="admin")

    svc.approve(pid, ver, actor="steward", simulation_id=sim.simulation_id)
    act = svc.activate(pid, ver, actor="admin", note="go live")
    assert act["status"] == "active"
    assert svc.get_active(pid) is not None

    # Second version for rollback target
    v2 = {
        **MINIMAL_DOC,
        "metadata": {**MINIMAL_DOC["metadata"], "version": "1.1.0", "description": "v2"},
        "rules": [
            {
                **MINIMAL_DOC["rules"][0],
                "id": "lac-network-v2",
                "reason": "Updated LAC demotion rule for v2.",
            }
        ],
    }
    c2 = svc.create(v2, actor="alice")
    sim2 = svc.simulate(
        policy_id=c2["id"],
        version=c2["version"],
        context={
            "column": "lac",
            "facts": {"column.name": "lac", "verdict.entity": "PHONE_NUMBER", "verdict.detected": True},
        },
    )
    svc.approve(c2["id"], c2["version"], actor="steward", simulation_id=sim2.simulation_id)
    svc.activate(c2["id"], c2["version"], actor="admin")
    assert svc.store.get_active_pointer(pid)["version"] == "1.1.0"

    rolled = svc.rollback(pid, actor="admin", note="revert")
    assert rolled["version"] == "1.0.0"
    assert svc.store.get_active_pointer(pid)["version"] == "1.0.0"

    svc.deactivate(pid, "1.0.0", actor="admin")
    assert svc.get_active(pid) is None


def test_promote_correction_creates_draft_only(svc):
    out = svc.promote_correction_to_draft(
        column="cell_lac",
        from_entity="PHONE_NUMBER",
        to_entity="NETWORK_ID",
        table="telecom.cells",
        run_id="run-1",
        actor="reviewer",
    )
    assert out["status"] == "draft"
    stored = svc.get(out["id"], out["version"])
    assert stored["lifecycle"]["approved"] is False
    assert svc.get_active(out["id"]) is None


def test_api_catalog(client):
    r = client.get("/api/behavior/catalog")
    assert r.status_code == 200
    body = r.json()
    assert "catalogue" in body
    assert body["manifest_sha256"]


def test_api_validate_and_lifecycle(client):
    r = client.post("/api/behavior/policies/validate", json={"document": MINIMAL_DOC})
    assert r.status_code == 200
    assert r.json()["ok"] is True

    r = client.post(
        "/api/behavior/policies",
        json={"document": MINIMAL_DOC, "actor": "alice"},
    )
    assert r.status_code == 200
    created = r.json()

    r = client.post(
        "/api/behavior/policies/simulate",
        json={
            "policy_id": created["id"],
            "version": created["version"],
            "context": {
                "column": "lac",
                "facts": {
                    "column.name": "lac",
                    "verdict.entity": "PHONE_NUMBER",
                    "verdict.detected": True,
                },
            },
        },
    )
    assert r.status_code == 200
    sim_id = r.json()["simulation_id"]

    r = client.post(
        f"/api/behavior/policies/{created['id']}/{created['version']}/approve",
        json={"actor": "steward", "simulation_id": sim_id},
    )
    assert r.status_code == 200

    r = client.post(
        f"/api/behavior/policies/{created['id']}/{created['version']}/activate",
        json={"actor": "admin", "note": "ship it"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "active"

    r = client.get("/api/behavior/metrics")
    assert r.status_code == 200
    assert r.json()["policies_active"] >= 1
