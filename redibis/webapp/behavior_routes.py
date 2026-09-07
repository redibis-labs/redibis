"""
redibis.webapp.behavior_routes
==============================
REST surface for Behavior Policy lifecycle (Phase 5+).

Mounted from backend.py via ``register_behavior_routes(app)``.
Thin adapter over ``redibis.services.behavior_service.BehaviorPolicyService``.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import HTTPException, Query
from pydantic import BaseModel

from redibis.services.behavior_service import (
    BehaviorPolicyService,
    BehaviorServiceError,
    behavior_service_from_env,
)


class ValidateBody(BaseModel):
    document: dict


class CreateBody(BaseModel):
    document: dict
    actor: str = ""
    note: str = ""


class SimulateBody(BaseModel):
    document: Optional[dict] = None
    policy_id: Optional[str] = None
    version: Optional[str] = None
    context: Optional[dict] = None
    contexts: Optional[list[dict]] = None


class ActorBody(BaseModel):
    actor: str
    role: str = "steward"
    note: str = ""
    simulation_id: Optional[str] = None


class RollbackBody(BaseModel):
    actor: str
    role: str = "admin"
    note: str = ""
    to_version: Optional[str] = None


class PromoteBody(BaseModel):
    column: str
    from_entity: str
    to_entity: str
    note: str = ""
    run_id: str = ""
    table: str = ""
    actor: str = ""
    policy_id: Optional[str] = None
    version: str = "0.1.0"


def register_behavior_routes(
    app: Any,
    service_factory=None,
) -> None:
    """Attach /api/behavior/* routes to the FastAPI app."""

    def _svc() -> BehaviorPolicyService:
        factory = service_factory or behavior_service_from_env
        return factory()

    def _call(fn, *a, **k):
        try:
            return fn(*a, **k)
        except BehaviorServiceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/behavior/catalog")
    def behavior_catalog() -> dict:
        return _svc().catalog()

    @app.post("/api/behavior/policies/validate")
    def validate_policy(body: ValidateBody) -> dict:
        return _svc().validate(body.document).to_dict()

    @app.get("/api/behavior/policies")
    def list_policies() -> dict:
        return {"policies": _svc().list_policies()}

    @app.post("/api/behavior/policies")
    def create_policy(body: CreateBody) -> dict:
        return _call(_svc().create, body.document, actor=body.actor, note=body.note)

    # Static suffixes MUST be registered before /{policy_id}/{version} or they
    # are shadowed (FastAPI matches in declaration order).
    @app.get("/api/behavior/policies/{policy_id}/active")
    def get_active(policy_id: str) -> dict:
        data = _svc().get_active(policy_id)
        if data is None:
            raise HTTPException(status_code=404, detail="no active policy")
        return data

    @app.get("/api/behavior/policies/{policy_id}/history")
    def policy_history(policy_id: str) -> dict:
        return {"events": _svc().store.list_lifecycle_events(policy_id)}

    @app.get("/api/behavior/policies/{policy_id}/{version}")
    def get_policy(policy_id: str, version: str) -> dict:
        return _call(_svc().get, policy_id, version)

    @app.post("/api/behavior/policies/simulate")
    def simulate_policy(body: SimulateBody) -> dict:
        result = _call(
            _svc().simulate,
            document=body.document,
            policy_id=body.policy_id,
            version=body.version,
            context=body.context,
            contexts=body.contexts,
        )
        return result.to_dict()

    @app.post("/api/behavior/policies/{policy_id}/{version}/approve")
    def approve_policy(policy_id: str, version: str, body: ActorBody) -> dict:
        return _call(
            _svc().approve,
            policy_id,
            version,
            actor=body.actor,
            role=body.role,
            note=body.note,
            simulation_id=body.simulation_id,
        )

    @app.post("/api/behavior/policies/{policy_id}/{version}/activate")
    def activate_policy(policy_id: str, version: str, body: ActorBody) -> dict:
        return _call(
            _svc().activate,
            policy_id,
            version,
            actor=body.actor,
            role=body.role or "admin",
            note=body.note,
            simulation_id=body.simulation_id,
        )

    @app.post("/api/behavior/policies/{policy_id}/{version}/deactivate")
    def deactivate_policy(policy_id: str, version: str, body: ActorBody) -> dict:
        return _call(
            _svc().deactivate,
            policy_id,
            version,
            actor=body.actor,
            role=body.role or "admin",
            note=body.note,
        )

    @app.post("/api/behavior/policies/{policy_id}/rollback")
    def rollback_policy(policy_id: str, body: RollbackBody) -> dict:
        return _call(
            _svc().rollback,
            policy_id,
            actor=body.actor,
            role=body.role,
            note=body.note,
            to_version=body.to_version,
        )

    @app.post("/api/behavior/policies/promote-correction")
    def promote_correction(body: PromoteBody) -> dict:
        return _call(
            _svc().promote_correction_to_draft,
            column=body.column,
            from_entity=body.from_entity,
            to_entity=body.to_entity,
            note=body.note,
            run_id=body.run_id,
            table=body.table,
            actor=body.actor,
            policy_id=body.policy_id,
            version=body.version,
        )

    @app.get("/api/behavior/audit")
    def behavior_audit(
        policy_id: Optional[str] = None,
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> dict:
        """List lifecycle events across policies (Phase 6 audit explorer backend)."""
        svc = _svc()
        events: list[dict] = []
        for item in svc.list_policies():
            pid = item["id"]
            if policy_id and pid != policy_id:
                continue
            for ev in svc.store.list_lifecycle_events(pid):
                events.append({"policy_id": pid, **ev})
        events.sort(key=lambda e: e.get("at") or "", reverse=True)
        return {"events": events[: int(limit)]}

    @app.get("/api/behavior/metrics")
    def behavior_metrics() -> dict:
        """Aggregate lifecycle/simulation counters (Phase 6 metrics)."""
        svc = _svc()
        policies = svc.list_policies()
        active = sum(1 for p in policies if p.get("active"))
        approved = sum(1 for p in policies if p.get("approved"))
        sims = 0
        for p in policies:
            data = svc.store.get(p["id"], p["version"]) or {}
            if (data.get("lifecycle") or {}).get("last_simulation_id"):
                sims += 1
        out = {
            "policies_total": len(policies),
            "policies_active": active,
            "policies_approved": approved,
            "versions_with_simulation": sims,
        }
        try:
            from redibis.behavior.learning import compute_rule_signals

            signals = compute_rule_signals(svc.store._base)
            out["learning"] = {
                "rules_with_labels": len(signals),
                "total_applications": sum(int(s.applications or 0) for s in signals),
                "total_confirmations": sum(int(s.confirmations or 0) for s in signals),
                "total_reversals": sum(int(s.reversals or 0) for s in signals),
            }
        except Exception:
            out["learning"] = {"rules_with_labels": 0}
        return out
