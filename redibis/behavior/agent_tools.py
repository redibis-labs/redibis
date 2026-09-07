"""
redibis.behavior.agent_tools
==============================
Pure-function typed tools for agent integration with the Behavior Policy Runtime.

These are plain Python functions — NOT LangGraph nodes, NOT CopilotKit actions.
Agent orchestration layers (redibis.agents.*) call these functions through the
typed tool surface. Direct library imports (GE, Presidio, etc.) remain behind
the registered engine adapters.

Safety invariants
-----------------
- Agents MUST NOT auto-activate LLM-generated policy drafts.
  ``tool_propose_draft`` returns a document with ``source: "llm_proposed"`` and
  ``lifecycle.status: "draft"``; a human approval step is REQUIRED before
  activation. This is documented and enforced by the store lifecycle checks.
- Simulation (``tool_simulate_policy``) is read-only and never writes.
- Validation (``tool_validate_policy``) runs lint/compile only — no activation.
- Every model call that feeds ``tool_propose_draft`` must go through
  ``redibis.telemetry.model_gateway.guarded_model_call``; that is the
  caller's responsibility, not this module's.
- This module has no dependency on CLI, webapp, LangGraph, or CopilotKit.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from redibis.behavior.store import BehaviorPolicyStore


# ---------------------------------------------------------------------------
# Tool: list policies
# ---------------------------------------------------------------------------

def tool_list_policies(store: BehaviorPolicyStore) -> list[dict]:
    """
    Return a summary list of all stored policies.

    Safe for agent consumption: returns id, version, active, engine, stage,
    description, status, and sha256 only.

    Usage by agents:
        Call before presenting policy options to a user or before proposing
        a draft to check for conflicts with active policies.
    """
    return store.list_policies()


# ---------------------------------------------------------------------------
# Tool: validate policy
# ---------------------------------------------------------------------------

def tool_validate_policy(
    store: BehaviorPolicyStore,
    document: dict,
) -> dict:
    """
    Compile and lint a policy document without activating it.

    Returns a result dict:
        {
            "valid": bool,
            "content_sha256": str,
            "findings": list[dict],   # lint findings (id/severity/message)
            "compiled_rule_count": int,
            "error": str | None,      # present on hard failure
        }

    Agents MUST present findings to users before proposing activation.
    """
    from redibis.behavior.compiler import compile_policy
    from redibis.behavior.lint import lint_policy, has_blocking_findings

    result: dict[str, Any] = {
        "valid": False,
        "content_sha256": "",
        "findings": [],
        "compiled_rule_count": 0,
        "error": None,
    }

    try:
        policy = compile_policy(document)
        findings = lint_policy(policy)
        result["content_sha256"] = policy.content_sha256
        result["compiled_rule_count"] = len(policy.rules)
        result["findings"] = [
            {
                "finding_id": f.finding_id,
                "severity": f.severity.value,
                "rule_id": f.rule_id,
                "message": f.message,
                "suggestion": f.suggestion,
            }
            for f in findings
        ]
        result["valid"] = not has_blocking_findings(findings)
    except Exception as exc:
        result["error"] = str(exc)

    return result


# ---------------------------------------------------------------------------
# Tool: simulate policy
# ---------------------------------------------------------------------------

def tool_simulate_policy(
    store: BehaviorPolicyStore,
    document: dict,
    contexts: list[dict],
) -> dict:
    """
    Evaluate a policy document against a list of serialized BehaviorContexts
    without writing anything.

    ``contexts`` is a list of dicts previously serialized from BehaviorContext.
    Returns:
        {
            "policy_sha256": str,
            "evaluated": int,
            "matched": int,
            "require_review": int,
            "blocked": int,
            "per_context": list[dict],   # context_hash, matched, patch_paths, reasons
            "error": str | None,
        }

    This call is fully read-only. Nothing is persisted.

    Agents MUST run simulation before proposing activation to users.
    """
    from redibis.behavior.compiler import compile_policy, compile_rules_for_evaluation
    from redibis.behavior.evaluator import PolicyEvaluator, sort_rules
    from redibis.behavior.reducer import PatchReducer
    from redibis.behavior.models import (
        BehaviorContext, BehaviorCapabilities, HookStage,
    )
    from redibis.behavior.registry import get_builtin_registry

    result: dict[str, Any] = {
        "policy_sha256": "",
        "evaluated": 0,
        "matched": 0,
        "require_review": 0,
        "blocked": 0,
        "per_context": [],
        "error": None,
    }

    try:
        policy = compile_policy(document)
        result["policy_sha256"] = policy.content_sha256
        compiled_rules = sort_rules(compile_rules_for_evaluation(policy))
        registry = get_builtin_registry()
        evaluator = PolicyEvaluator(registry=registry)

        for ctx_dict in contexts:
            facts = dict(ctx_dict.get("facts") or {})
            facts = {k: v for k, v in facts.items() if not str(k).startswith("raw.")}
            caps_data = ctx_dict.get("capabilities") or {}
            caps = BehaviorCapabilities(
                readable_facts=frozenset(caps_data.get("readable_facts") or facts.keys()),
                allowed_actions=frozenset(
                    caps_data.get("allowed_actions")
                    or registry.catalogue()["effects"].keys()
                ),
                allowed_patch_operations=frozenset(
                    caps_data.get("allowed_patch_operations")
                    or {
                        "verdict.detected", "verdict.entity", "verdict.confidence",
                        "review.require", "review.add_reason",
                    }
                ),
            )
            try:
                stage = HookStage(ctx_dict.get("stage") or policy.stage.value)
            except ValueError:
                stage = policy.stage

            ctx = BehaviorContext(
                run_id=str(ctx_dict.get("run_id") or ""),
                table=str(ctx_dict.get("table") or ""),
                column=ctx_dict.get("column"),
                engine=str(ctx_dict.get("engine") or policy.engine),
                stage=stage,
                facts=facts,
                capabilities=caps,
                registry_manifest_sha256=registry.manifest_sha256,
                environment=ctx_dict.get("environment"),
                jurisdiction=ctx_dict.get("jurisdiction"),
            )

            traces, proposed = evaluator.evaluate(compiled_rules, ctx, stage=stage)
            reducer = PatchReducer(caps)
            applied, _rej, blocked = reducer.reduce(proposed)
            result["evaluated"] += 1
            matched = sum(
                1 for r in traces
                if getattr(r.status, "value", str(r.status)) == "matched"
            )
            if matched:
                result["matched"] += 1
            if applied.require_review:
                result["require_review"] += 1
            result["blocked"] += len(blocked)

            context_hash = hashlib.sha256(
                json.dumps(dict(facts), sort_keys=True).encode()
            ).hexdigest()[:16]

            result["per_context"].append({
                "context_hash": context_hash,
                "matched": matched,
                "patch_paths": [op.path for op in applied.operations],
                "require_review": applied.require_review,
                "reasons": list(applied.reasons),
                "blocked": len(blocked),
            })

    except Exception as exc:
        result["error"] = str(exc)

    return result


# ---------------------------------------------------------------------------
# Tool: propose draft
# ---------------------------------------------------------------------------

def tool_propose_draft(
    correction_examples: list[dict],
    *,
    engine: str,
    stage: str = "post_verdict",
    policy_id: Optional[str] = None,
    description: str = "",
    source_stats: Optional[dict] = None,
) -> dict:
    """
    Build a draft policy document from a list of correction examples.

    ``correction_examples`` is a list of dicts with:
        column, original_verdict, desired_verdict, reason

    Returns a draft policy document dict with:
        source: "llm_proposed"
        lifecycle.status: "draft"
        lifecycle.requires_human_approval: true
        scope: narrowed to observed columns/tables if determinable

    IMPORTANT — agents MUST NOT auto-activate this draft.
    The returned document has ``lifecycle.requires_human_approval: true``.
    Activation requires human approval through the store lifecycle.

    Source statistics (reversal rate, correction count, etc.) are embedded
    as ``lifecycle.source_stats`` for audit/simulation transparency.
    """
    rules = []
    seen_columns: set[str] = set()
    seen_tables: set[str] = set()

    for i, ex in enumerate(correction_examples[:50]):  # cap at 50 examples
        col = str(ex.get("column") or "")
        table = str(ex.get("table") or "")
        original = ex.get("original_verdict")
        desired = ex.get("desired_verdict")
        reason = str(ex.get("reason") or "behavior correction draft")[:512]

        if col:
            seen_columns.add(col)
        if table:
            seen_tables.add(table)

        if not col or original is None or desired is None:
            continue

        effects: list[dict] = []
        desired_bool: Optional[bool] = None
        if isinstance(desired, bool):
            desired_bool = desired
        elif isinstance(desired, str) and desired.lower() in ("true", "false"):
            desired_bool = desired.lower() == "true"

        if engine == "pii" and desired_bool is not None:
            effects.append({
                "effect": "core.verdict.set_detected",
                "params": {"detected": desired_bool},
            })
            if not desired_bool:
                effects.append({
                    "effect": "core.verdict.set_entity",
                    "params": {"entity": None},
                })
        elif engine == "quality" and isinstance(desired, str):
            effects.append({
                "effect": "quality.proposal.change_severity",
                "params": {"severity": desired},
            })
        elif engine == "classification" and isinstance(desired, str):
            if str(original) and str(original) != desired:
                effects.append({
                    "effect": "classification.candidate.drop_tag",
                    "params": {"tag": str(original)},
                })
            effects.append({
                "effect": "classification.candidate.add_tag",
                "params": {"tag": desired},
            })

        effects.append({
            "effect": "core.review.require",
            "params": {"role": "steward"},
        })

        if not effects:
            continue

        rules.append({
            "id": f"draft_rule_{i:03d}",
            "when": {
                "all": [
                    {"fact": "column.name", "op": "eq", "value": col},
                ]
            },
            "effects": effects,
            "reason": reason,
            "priority": 100 + i,
            "terminal": False,
        })

    scope: dict[str, Any] = {}
    if seen_columns:
        scope["columns"] = sorted(seen_columns)
    if seen_tables:
        scope["tables"] = sorted(seen_tables)

    draft: dict[str, Any] = {
        "id": policy_id or f"draft_{engine}_{stage}",
        "version": "0.1.0-draft",
        "description": description or f"Draft policy from {len(correction_examples)} correction examples",
        "engine": engine,
        "stage": stage,
        "source": "llm_proposed",
        "owner": "agent_draft",
        "scope": scope,
        "rules": rules,
        "lifecycle": {
            "status": "draft",
            "requires_human_approval": True,
            "requires_simulation": True,
            "note": (
                "This draft was generated from correction examples. "
                "It MUST be reviewed, validated, simulated, and approved "
                "by a human before activation. Do NOT auto-activate."
            ),
        },
    }

    if source_stats:
        draft["lifecycle"]["source_stats"] = {
            k: v for k, v in source_stats.items()
            if isinstance(v, (int, float, str, bool, type(None)))
        }

    return draft
