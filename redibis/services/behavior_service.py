"""
redibis.services.behavior_service
=================================
Governed lifecycle for Behavior Policies (Phase 5).

Responsibilities
----------------
- Catalogue / validate / lint
- Immutable create / list / get
- Simulation (write-free)
- Approve / activate / deactivate / rollback
- Promote human corrections to draft policies

Never writes to ContractStore.active. Simulation never persists contracts.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from redibis.behavior.compiler import compile_policy, compile_rules_for_evaluation
from redibis.behavior.evaluator import PolicyEvaluator, sort_rules
from redibis.behavior.lint import has_blocking_findings, lint_policy
from redibis.behavior.models import (
    BehaviorCapabilities,
    BehaviorContext,
    BehaviorPatch,
    BehaviorPolicy,
    HookStage,
    PolicySource,
    RuntimeMode,
)
from redibis.behavior.reducer import PatchReducer
from redibis.behavior.registry import get_builtin_registry
from redibis.behavior.store import BehaviorPolicyStore


class BehaviorServiceError(ValueError):
    """User/input error for behavior policy operations."""


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z"


def default_policy_store_dir() -> Path:
    """Resolve the policy store root (configs/behavior-policies by default)."""
    base = os.environ.get("BEHAVIOR_POLICIES_DIR") or os.environ.get("CONFIGS_DIR") or "./configs"
    return Path(base) / "behavior-policies"


@dataclass
class ValidationResult:
    ok: bool
    content_sha256: str = ""
    policy_id: str = ""
    version: str = ""
    engine: str = ""
    stage: str = ""
    effective_rule_order: list[str] = field(default_factory=list)
    lint: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    eligible_for_activation: bool = False
    preview: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "content_sha256": self.content_sha256,
            "policy_id": self.policy_id,
            "version": self.version,
            "engine": self.engine,
            "stage": self.stage,
            "effective_rule_order": self.effective_rule_order,
            "lint": self.lint,
            "errors": self.errors,
            "eligible_for_activation": self.eligible_for_activation,
            "preview": self.preview,
        }


@dataclass
class SimulationResult:
    matches: list[dict] = field(default_factory=list)
    non_matches: list[dict] = field(default_factory=list)
    unknown: list[dict] = field(default_factory=list)
    proposed: dict = field(default_factory=dict)
    applied: dict = field(default_factory=dict)
    blocked: list[dict] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    before: dict = field(default_factory=dict)
    after: dict = field(default_factory=dict)
    aggregate: dict = field(default_factory=dict)
    simulation_id: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class BehaviorPolicyService:
    """Backend product surface for the behavior policy lifecycle."""

    def __init__(
        self,
        store: Optional[BehaviorPolicyStore] = None,
        *,
        base_dir: Optional[str | Path] = None,
        require_approval_for_activation: bool = True,
        require_simulation_for_activation: bool = True,
    ) -> None:
        self.store = store or BehaviorPolicyStore(base_dir or default_policy_store_dir())
        self.require_approval = require_approval_for_activation
        self.require_simulation = require_simulation_for_activation
        self._simulations: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Catalogue
    # ------------------------------------------------------------------

    def catalog(self) -> dict:
        reg = get_builtin_registry()
        return {
            "manifest_sha256": reg.manifest_sha256,
            "catalogue": reg.catalogue(),
            "stages": [s.value for s in HookStage],
            "sources": [s.value for s in PolicySource],
            "modes": [m.value for m in RuntimeMode],
        }

    # ------------------------------------------------------------------
    # Validate
    # ------------------------------------------------------------------

    def validate(self, document: Mapping[str, Any]) -> ValidationResult:
        errors: list[str] = []
        try:
            policy = compile_policy(dict(document), source=PolicySource.USER)
        except Exception as exc:
            return ValidationResult(ok=False, errors=[f"compile: {exc}"])

        reg = get_builtin_registry()
        for rule in policy.rules:
            for effect in rule.effects:
                if not reg.has_effect(effect.effect):
                    errors.append(
                        f"rule {rule.id!r}: unknown effect {effect.effect!r}"
                    )

        findings = lint_policy(policy)
        lint_dicts = [
            {
                "id": f.finding_id,
                "finding_id": f.finding_id,
                "severity": f.severity.value if hasattr(f.severity, "value") else str(f.severity),
                "message": f.message,
                "rule_id": f.rule_id,
                "related_rule_ids": list(getattr(f, "related_rule_ids", ()) or ()),
                "suggestion": getattr(f, "suggestion", "") or "",
            }
            for f in findings
        ]
        blocking = has_blocking_findings(findings)
        if blocking:
            errors.append("blocking lint findings present — not eligible for activation")

        compiled = sort_rules(compile_rules_for_evaluation(policy))
        order = [r.id for r in compiled]

        ok = not errors
        return ValidationResult(
            ok=ok and not blocking,
            content_sha256=policy.content_sha256,
            policy_id=policy.id,
            version=policy.version,
            engine=policy.engine,
            stage=policy.stage.value,
            effective_rule_order=order,
            lint=lint_dicts,
            errors=errors,
            eligible_for_activation=ok and not blocking,
            preview={
                "id": policy.id,
                "version": policy.version,
                "description": policy.description,
                "rule_count": len(policy.rules),
                "content_sha256": policy.content_sha256,
            },
        )

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create(
        self,
        document: Mapping[str, Any],
        *,
        actor: str = "",
        note: str = "",
        source: PolicySource = PolicySource.USER,
    ) -> dict:
        validation = self.validate(document)
        if not validation.content_sha256:
            raise BehaviorServiceError("; ".join(validation.errors) or "invalid policy")

        policy = compile_policy(dict(document), source=source)
        try:
            self.store.save(
                policy,
                document=dict(document),
                lifecycle={
                    "status": "draft",
                    "created_at": _utc_iso(),
                    "created_by": actor or "system",
                    "note": note,
                    "approved": False,
                    "last_simulation_id": None,
                },
            )
        except ValueError as exc:
            raise BehaviorServiceError(str(exc)) from exc

        return {
            "id": policy.id,
            "version": policy.version,
            "content_sha256": policy.content_sha256,
            "status": "draft",
            "validation": validation.to_dict(),
        }

    def list_policies(self) -> list[dict]:
        return self.store.list_policies()

    def get(self, policy_id: str, version: str) -> dict:
        data = self.store.get(policy_id, version)
        if data is None:
            raise BehaviorServiceError(f"policy ({policy_id}, {version}) not found")
        return data

    def get_active(self, policy_id: str) -> Optional[dict]:
        return self.store.get_active(policy_id)

    # ------------------------------------------------------------------
    # Simulation
    # ------------------------------------------------------------------

    def simulate(
        self,
        *,
        document: Optional[Mapping[str, Any]] = None,
        policy_id: Optional[str] = None,
        version: Optional[str] = None,
        contexts: Optional[Sequence[Mapping[str, Any]]] = None,
        context: Optional[Mapping[str, Any]] = None,
    ) -> SimulationResult:
        """Evaluate a policy against sanitized context(s). Never writes contracts."""
        policy = self._resolve_policy(document=document, policy_id=policy_id, version=version)
        ctx_list = list(contexts or [])
        if context:
            ctx_list.append(dict(context))
        if not ctx_list:
            raise BehaviorServiceError("simulation requires at least one sanitized context")

        compiled = sort_rules(compile_rules_for_evaluation(policy))
        evaluator = PolicyEvaluator(registry=get_builtin_registry())
        sim_id = hashlib.sha256(
            f"{policy.id}:{policy.version}:{_utc_iso()}".encode()
        ).hexdigest()[:16]

        matches: list[dict] = []
        non_matches: list[dict] = []
        unknown: list[dict] = []
        blocked_all: list[dict] = []
        conflicts: list[dict] = []
        column_results: list[dict] = []
        changed = 0
        last_proposed = BehaviorPatch()
        last_applied = BehaviorPatch()

        for raw in ctx_list:
            facts = dict(raw.get("facts") or {})
            facts = {k: v for k, v in facts.items() if not str(k).startswith("raw.")}
            stage = HookStage(raw.get("stage") or policy.stage.value)
            caps = BehaviorCapabilities(
                readable_facts=frozenset(facts.keys()),
                allowed_actions=frozenset(get_builtin_registry().catalogue()["effects"].keys()),
                allowed_patch_operations=frozenset({
                    "verdict.detected", "verdict.entity", "verdict.confidence",
                    "review.require", "review.add_reason",
                    "threshold.presidio_min", "threshold.gliner_min", "threshold.phone_min",
                }),
            )
            ctx = BehaviorContext(
                run_id=str(raw.get("run_id") or "simulate"),
                table=str(raw.get("table") or ""),
                column=str(raw.get("column") or ""),
                engine=str(raw.get("engine") or policy.engine),
                stage=stage,
                facts=facts,
                capabilities=caps,
                registry_manifest_sha256=get_builtin_registry().manifest_sha256,
            )
            traces, proposed = evaluator.evaluate(compiled, ctx, stage=stage)
            reducer = PatchReducer(caps)
            applied, _rejected, blocked = reducer.reduce(proposed)
            last_proposed, last_applied = proposed, applied

            before = {
                "detected": facts.get("verdict.detected"),
                "entity": facts.get("verdict.entity"),
                "confidence": facts.get("verdict.confidence"),
            }
            after = dict(before)
            for op in applied.operations:
                if op.path == "verdict.detected":
                    after["detected"] = op.value
                elif op.path == "verdict.entity":
                    after["entity"] = op.value
                elif op.path == "verdict.confidence":
                    after["confidence"] = op.value
            if before != after:
                changed += 1

            for t in traces:
                entry = {
                    "column": ctx.column,
                    "rule_id": t.rule_id,
                    "status": t.status.value if hasattr(t.status, "value") else str(t.status),
                    "reason": getattr(t, "reason", "") or "",
                }
                status = entry["status"]
                if status == "matched":
                    matches.append(entry)
                elif status in ("unknown", "unknown_no_match"):
                    unknown.append(entry)
                elif status in ("condition_false", "no_match", "skipped", "out_of_scope", "shadowed"):
                    non_matches.append(entry)

            for b in blocked:
                blocked_all.append({
                    "column": ctx.column,
                    "path": b.path,
                    "blocked_by": b.blocked_by,
                    "reason": b.reason,
                })
                if "conflict" in (b.blocked_by or "").lower() or "same_authority" in (b.blocked_by or ""):
                    conflicts.append({
                        "column": ctx.column,
                        "path": b.path,
                        "reason": b.reason,
                    })

            column_results.append({
                "column": ctx.column,
                "before": before,
                "after": after,
                "require_review": applied.require_review,
            })

        result = SimulationResult(
            matches=matches,
            non_matches=non_matches,
            unknown=unknown,
            proposed=_patch_dict(last_proposed),
            applied=_patch_dict(last_applied),
            blocked=blocked_all,
            conflicts=conflicts,
            before=column_results[0]["before"] if len(column_results) == 1 else {},
            after=column_results[0]["after"] if len(column_results) == 1 else {},
            aggregate={
                "contexts": len(ctx_list),
                "matches": len(matches),
                "unknown": len(unknown),
                "blocked": len(blocked_all),
                "conflicts": len(conflicts),
                "verdict_changes": changed,
                "columns": column_results,
            },
            simulation_id=sim_id,
        )
        self._simulations[sim_id] = {
            "policy_id": policy.id,
            "version": policy.version,
            "content_sha256": policy.content_sha256,
            "at": _utc_iso(),
            "aggregate": result.aggregate,
        }
        self.store.record_simulation(
            policy.id,
            policy.version,
            sim_id,
            result.aggregate,
            content_sha256=policy.content_sha256,
        )
        return result

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _assert_valid_simulation(
        self,
        policy_id: str,
        version: str,
        data: Mapping[str, Any],
        simulation_id: Optional[str],
    ) -> str:
        """Validate a simulation receipt belongs to this policy version/content."""
        if not simulation_id:
            raise BehaviorServiceError(
                "approval/activation requires a successful simulation reference "
                "(pass simulation_id or run simulate first)"
            )
        receipt = self._simulations.get(simulation_id)
        if receipt is None:
            receipt = self.store.get_simulation_receipt(policy_id, simulation_id)
        if receipt is None:
            raise BehaviorServiceError(
                f"unknown or forged simulation_id {simulation_id!r}"
            )
        if receipt.get("policy_id") != policy_id or receipt.get("version") != version:
            raise BehaviorServiceError(
                "simulation_id does not match this policy id/version"
            )
        expected_sha = data.get("content_sha256") or ""
        receipt_sha = receipt.get("content_sha256") or ""
        if expected_sha and receipt_sha and expected_sha != receipt_sha:
            raise BehaviorServiceError(
                "simulation_id does not match current policy content hash"
            )
        return simulation_id

    def approve(
        self,
        policy_id: str,
        version: str,
        *,
        actor: str,
        role: str = "steward",
        note: str = "",
        simulation_id: Optional[str] = None,
    ) -> dict:
        data = self.get(policy_id, version)
        lifecycle = data.get("lifecycle") or {}
        sim_ref = simulation_id or lifecycle.get("last_simulation_id")
        if self.require_simulation:
            sim_ref = self._assert_valid_simulation(policy_id, version, data, sim_ref)
        event = {
            "action": "approve",
            "actor": actor,
            "role": role,
            "note": note,
            "at": _utc_iso(),
            "content_sha256": data.get("content_sha256"),
            "simulation_id": sim_ref,
        }
        self.store.set_lifecycle(
            policy_id,
            version,
            {
                **lifecycle,
                "status": "approved",
                "approved": True,
                "approved_by": actor,
                "approved_role": role,
                "approved_at": event["at"],
                "approve_note": note,
                "last_simulation_id": sim_ref,
            },
        )
        self.store.append_lifecycle_event(policy_id, event)
        return {"id": policy_id, "version": version, "status": "approved", "event": event}

    def activate(
        self,
        policy_id: str,
        version: str,
        *,
        actor: str,
        role: str = "admin",
        note: str = "",
        simulation_id: Optional[str] = None,
        skip_approval_check: bool = False,
        skip_stack_push: bool = False,
    ) -> dict:
        data = self.get(policy_id, version)
        lifecycle = data.get("lifecycle") or {}
        if self.require_approval and not skip_approval_check and not lifecycle.get("approved"):
            raise BehaviorServiceError(
                f"policy ({policy_id}, {version}) is not approved — approve before activate"
            )
        document = data.get("document")
        if not document:
            raise BehaviorServiceError("stored policy is missing original document")
        validation = self.validate(document)
        if not validation.eligible_for_activation:
            raise BehaviorServiceError(
                "policy has blocking validation/lint findings and cannot be activated: "
                + "; ".join(validation.errors or ["ineligible"])
            )
        sim_ref = simulation_id or lifecycle.get("last_simulation_id")
        if self.require_simulation and not skip_approval_check:
            # Rollback / already-approved reactivation still needs a recorded sim
            # unless the version was previously activated with one.
            if not lifecycle.get("activated_at"):
                sim_ref = self._assert_valid_simulation(policy_id, version, data, sim_ref)
        prior = self.store.get_active_pointer(policy_id)
        if skip_stack_push:
            # Direct pointer write without stacking (used by explicit to_version rollback)
            from redibis.behavior.ids import validate_policy_id, validate_policy_version
            import json as _json

            pointer = {
                "id": validate_policy_id(policy_id),
                "version": validate_policy_version(version),
                "sha256": data["content_sha256"],
            }
            policy_dir = self.store._policy_dir(policy_id)
            (policy_dir / "active.json").write_text(
                _json.dumps(pointer, indent=2), encoding="utf-8"
            )
        else:
            self.store.activate(policy_id, version)
        event = {
            "action": "activate",
            "actor": actor,
            "role": role,
            "note": note,
            "at": _utc_iso(),
            "prior_sha": (prior or {}).get("sha256"),
            "new_sha": data.get("content_sha256"),
            "prior_version": (prior or {}).get("version"),
            "new_version": version,
            "simulation_id": sim_ref,
        }
        self.store.set_lifecycle(
            policy_id,
            version,
            {
                **lifecycle,
                "status": "active",
                "activated_by": actor,
                "activated_at": event["at"],
                "last_simulation_id": sim_ref or lifecycle.get("last_simulation_id"),
            },
        )
        self.store.append_lifecycle_event(policy_id, event)
        return {"id": policy_id, "version": version, "status": "active", "event": event}

    def deactivate(
        self,
        policy_id: str,
        version: str,
        *,
        actor: str,
        role: str = "admin",
        note: str = "",
    ) -> dict:
        prior = self.store.get_active_pointer(policy_id)
        self.store.deactivate(policy_id)
        event = {
            "action": "deactivate",
            "actor": actor,
            "role": role,
            "note": note,
            "at": _utc_iso(),
            "prior_sha": (prior or {}).get("sha256"),
            "prior_version": (prior or {}).get("version"),
            "target_version": version,
        }
        self.store.append_lifecycle_event(policy_id, event)
        if prior and prior.get("version") == version:
            data = self.store.get(policy_id, version) or {}
            lc = dict(data.get("lifecycle") or {})
            lc["status"] = "approved" if lc.get("approved") else "draft"
            self.store.set_lifecycle(policy_id, version, lc)
        return {"id": policy_id, "version": version, "status": "inactive", "event": event}

    def rollback(
        self,
        policy_id: str,
        *,
        actor: str,
        role: str = "admin",
        note: str = "",
        to_version: Optional[str] = None,
    ) -> dict:
        """Change the active pointer to a prior version. Immutable history is preserved."""
        if to_version is None:
            try:
                pointer = self.store.rollback_to_previous(policy_id)
            except FileNotFoundError as exc:
                raise BehaviorServiceError(str(exc)) from exc
            to_version = pointer["version"]
            data = self.get(policy_id, to_version)
            lc = dict(data.get("lifecycle") or {})
            event = {
                "action": "rollback",
                "actor": actor,
                "role": role,
                "note": note or f"rollback to {to_version}",
                "at": _utc_iso(),
                "new_version": to_version,
                "new_sha": data.get("content_sha256"),
            }
            self.store.set_lifecycle(
                policy_id,
                to_version,
                {
                    **lc,
                    "status": "active",
                    "activated_by": actor,
                    "activated_at": event["at"],
                },
            )
            self.store.append_lifecycle_event(policy_id, event)
            return {
                "id": policy_id,
                "version": to_version,
                "status": "active",
                "event": event,
            }

        # Explicit target: re-point without pushing current onto the stack
        data = self.get(policy_id, to_version)
        lc = dict(data.get("lifecycle") or {})
        if not lc.get("approved"):
            raise BehaviorServiceError(
                f"cannot roll back to unapproved version {to_version!r}"
            )
        return self.activate(
            policy_id,
            to_version,
            actor=actor,
            role=role,
            note=note or f"rollback to {to_version}",
            skip_approval_check=True,
            skip_stack_push=True,
        )

    # ------------------------------------------------------------------
    # Promote correction → draft
    # ------------------------------------------------------------------

    def promote_correction_to_draft(
        self,
        *,
        column: str,
        from_entity: str,
        to_entity: str,
        note: str = "",
        run_id: str = "",
        table: str = "",
        actor: str = "",
        policy_id: Optional[str] = None,
        version: str = "0.1.0",
    ) -> dict:
        """Build a narrow draft BehaviorPolicy from an approved human correction."""
        from redibis.classification.edge_rules import suggest_rule_from_correction

        edge = suggest_rule_from_correction(
            column=column,
            from_entity=from_entity,
            to_entity=to_entity,
            note=note,
        )
        when = edge.get("when") or {}
        then = edge.get("then") or {}
        reason = (
            note
            or then.get("note")
            or f"Promoted correction {column}: {from_entity} → {to_entity}"
        )
        # Provenance is recorded in description only — not executable conditions.
        desc = (
            f"Draft from correction on {table}.{column} "
            f"(run={run_id or '-'}; {from_entity}→{to_entity})"
        )
        predicates = []
        if when.get("name_matches"):
            predicates.append({
                "fact": "column.name", "op": "matches", "value": when["name_matches"]
            })
        if when.get("entity"):
            predicates.append({
                "fact": "verdict.entity", "op": "eq", "value": str(when["entity"])
            })
        cond: dict = predicates[0] if len(predicates) == 1 else {"all": predicates}

        effects: list[dict] = []
        se = then.get("set_entity")
        if se == "NOT_PII":
            effects.append({"effect": "core.verdict.set_detected", "params": {"detected": False}})
            effects.append({"effect": "core.verdict.set_entity", "params": {"entity": None}})
        elif se:
            effects.append({"effect": "core.verdict.set_entity", "params": {"entity": str(se)}})
            effects.append({"effect": "core.verdict.set_detected", "params": {"detected": True}})
        if then.get("require_review"):
            effects.append({"effect": "core.review.require", "params": {"role": "steward"}})
        effects.append({"effect": "core.review.add_reason", "params": {"reason": reason[:512]}})

        document = {
            "apiVersion": "redibis.io/behavior-policy/v1",
            "kind": "BehaviorPolicy",
            "metadata": {
                "id": policy_id or f"draft_promoted_{_slug(column)}",
                "version": version,
                "description": desc[:512],
                "owner": actor or "system",
            },
            "appliesTo": {"engine": "pii", "stage": "post_verdict"},
            "rules": [
                {
                    "id": str(edge.get("id") or f"promoted_{_slug(column)}"),
                    "when": cond,
                    "effects": effects,
                    "reason": reason[:512],
                    "priority": 100,
                    "terminal": True,
                }
            ],
        }
        return self.create(
            document,
            actor=actor or "system",
            note=note or f"Draft from correction on {table}.{column}",
            source=PolicySource.USER,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_policy(
        self,
        *,
        document: Optional[Mapping[str, Any]],
        policy_id: Optional[str],
        version: Optional[str],
    ) -> BehaviorPolicy:
        if document is not None:
            return compile_policy(dict(document), source=PolicySource.USER)
        if not policy_id or not version:
            raise BehaviorServiceError("provide document or policy_id+version")
        data = self.get(policy_id, version)
        doc = data.get("document")
        if not doc:
            raise BehaviorServiceError(
                "stored policy is missing original document — re-create the policy"
            )
        return compile_policy(doc, source=PolicySource.USER)


def _patch_dict(patch: BehaviorPatch) -> dict:
    return {
        "operations": [
            {
                "op": op.op,
                "path": op.path,
                "value": op.value,
                "authority": op.authority.name if hasattr(op.authority, "name") else str(op.authority),
                "rule_id": op.rule_id,
                "reason": op.reason,
            }
            for op in patch.operations
        ],
        "require_review": patch.require_review,
        "review_role": patch.review_role,
        "reasons": list(patch.reasons),
    }


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_") or "col"


def behavior_service_from_env() -> BehaviorPolicyService:
    return BehaviorPolicyService()
