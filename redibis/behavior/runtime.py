"""
redibis.behavior.runtime
========================
Orchestrate Behavior Policy evaluation for a scan step.

Default: disabled — existing edge-rule path is unchanged.
Shadow mode: evaluate the new runtime without applying patches; emit parity
warnings when results diverge from the legacy edge-rule outcome.
Active mode: apply the reduced BehaviorPatch (compatibility edge_rules converted
to BehaviorPolicy when no explicit behavior policies are configured).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, List, Mapping, Optional, Sequence

from redibis.behavior.config import BehaviorConfig
from redibis.behavior.models import (
    Authority,
    BehaviorPatch,
    BehaviorPolicy,
    BehaviorPolicyWarning,
    FailMode,
    HookStage,
    RuntimeMode,
)
from redibis.models import PIIDetection

log = logging.getLogger("redibis.behavior.runtime")


@dataclass
class BehaviorApplyResult:
    """Outcome of applying (or shadowing) behavior policy for one column batch."""

    detections: List[PIIDetection]
    warnings: list[BehaviorPolicyWarning] = field(default_factory=list)
    shadow_mismatches: int = 0


@dataclass
class ThresholdApplyResult:
    """Outcome of pre_verdict threshold policy evaluation."""

    thresholds: Any
    warnings: list[BehaviorPolicyWarning] = field(default_factory=list)
    applied: bool = False
    baseline_thresholds: Any = None


def _load_behavior_config() -> BehaviorConfig:
    try:
        from redibis.config import RedibisConfig

        cfg = RedibisConfig.load()
        return getattr(cfg, "behavior", None) or BehaviorConfig()
    except Exception:
        return BehaviorConfig()


def _compat_policy_from_edge_pack(
    pack_name: str,
    overlay: Optional[list] = None,
) -> Optional[BehaviorPolicy]:
    from redibis.behavior.adapters.pii import edge_rules_to_behavior_policy
    from redibis.classification.edge_rules import merge_policy_edge_rules
    from redibis.classification.pack_store import load_pack

    policy = load_pack(pack_name or "telecom")
    if overlay:
        policy = merge_policy_edge_rules(policy, overlay)
    return edge_rules_to_behavior_policy(policy, policy_id=f"edge_compat:{pack_name}")


def _try_load_pii_decisions(table: str) -> Optional[dict[str, dict]]:
    """Best-effort load of durable PII overlays without importing the webapp."""
    if not table:
        return None
    try:
        import os

        from redibis.store.contract_store import ContractStore
        from redibis.store.storage_backend import LocalBackend, get_backend

        if os.getenv("USE_LOCAL_STORAGE", "false").lower() == "true":
            backend = LocalBackend(os.getenv("LOCAL_STORAGE_ROOT", "./_local_storage"))
        else:
            backend = get_backend(mode="auto")
        bucket = os.getenv("S3_CONTRACTS_BUCKET", "active-contracts")
        store = ContractStore(backend=backend, bucket=bucket)
        return store.get_pii_decisions(table)
    except Exception as exc:
        log.debug("pii decisions unavailable for %s: %s", table, exc)
        return None


def _resolve_registry(cfg: BehaviorConfig):
    from redibis.behavior.registry import BehaviorRegistry, get_builtin_registry

    base = get_builtin_registry()
    # Copy so plugin registration does not permanently mutate the singleton.
    registry = BehaviorRegistry()
    registry._facts = dict(base._facts)
    registry._operators = dict(base._operators)
    registry._effects = dict(base._effects)
    registry._actions = dict(base._actions)
    registry._builtin_facts = frozenset(base._builtin_facts)
    registry._builtin_ops = frozenset(base._builtin_ops)
    registry._builtin_effects = frozenset(base._builtin_effects)
    registry._fact_providers = dict(getattr(base, "_fact_providers", {}) or {})
    registry._invalidate_manifest()

    if cfg.plugin_allowlist:
        from redibis.behavior.plugins import apply_plugins_to_registry

        apply_plugins_to_registry(cfg, registry)
    return registry


def _enrich_facts_from_providers(ctx, registry):
    """Return a context with missing facts filled from registered providers."""
    providers = getattr(registry, "_fact_providers", None) or {}
    if not providers:
        return ctx
    facts = dict(ctx.facts)
    changed = False
    for fact_id, provider in providers.items():
        if fact_id in facts or provider is None:
            continue
        try:
            value = provider(replace(ctx, facts=facts) if changed else ctx)
        except Exception as exc:
            log.debug("fact provider %s failed: %s", fact_id, exc)
            continue
        if value is not None:
            facts[fact_id] = value
            changed = True
    if not changed:
        return ctx
    return replace(ctx, facts=facts)


def resolve_policies_for_stage(
    cfg: BehaviorConfig,
    *,
    engine: str,
    stage: HookStage,
    policy_pack: str = "telecom",
    edge_rules_overlay: Optional[list] = None,
) -> list[BehaviorPolicy]:
    """
    Resolve compiled policies for an engine/stage.

    Preference order:
      1. Explicit ``behavior.policies`` file paths / store ids
      2. Active policies in the behavior policy store
      3. Edge-rule compatibility conversion (post_verdict PII only)
    """
    from redibis.behavior.compiler import compile_policy

    found: list[BehaviorPolicy] = []
    seen: set[str] = set()

    def _add(pol: Optional[BehaviorPolicy]) -> None:
        if pol is None:
            return
        key = f"{pol.id}@{pol.version}:{pol.content_sha256}"
        if key in seen:
            return
        if pol.engine != engine or pol.stage is not stage:
            return
        seen.add(key)
        found.append(pol)

    # Configured policy refs (paths or store ids)
    for ref in cfg.policies or []:
        ref_s = str(ref)
        path = Path(ref_s)
        try:
            if path.exists() and path.is_file():
                doc = json.loads(path.read_text(encoding="utf-8"))
                _add(compile_policy(doc))
                continue
        except Exception as exc:
            log.warning("behavior policy path %s skipped: %s", ref_s, exc)

        # Treat as store policy id → active version
        try:
            from redibis.services.behavior_service import default_policy_store_dir
            from redibis.behavior.store import BehaviorPolicyStore

            store = BehaviorPolicyStore(default_policy_store_dir())
            data = store.get_active(ref_s)
            if data and data.get("document"):
                _add(compile_policy(data["document"]))
        except Exception as exc:
            log.debug("behavior policy id %s skipped: %s", ref_s, exc)

    # All active store policies for this engine/stage
    try:
        from redibis.services.behavior_service import default_policy_store_dir
        from redibis.behavior.store import BehaviorPolicyStore

        store = BehaviorPolicyStore(default_policy_store_dir())
        for data in store.list_active_policies(engine=engine, stage=stage.value):
            doc = data.get("document")
            if doc:
                _add(compile_policy(doc))
    except Exception as exc:
        log.debug("active store policies unavailable: %s", exc)

    if found:
        return found

    if engine == "pii" and stage is HookStage.POST_VERDICT:
        compat = _compat_policy_from_edge_pack(policy_pack, edge_rules_overlay)
        if compat is not None:
            return [compat]
    return []


def _human_for_column(
    column: str,
    pii_decisions: Optional[Mapping[str, Mapping[str, Any]]],
) -> dict[str, Authority]:
    from redibis.behavior.adapters.pii import human_authority_for_decision

    if not pii_decisions:
        return {}
    return human_authority_for_decision(pii_decisions.get(column))


def _checksum_blocks_patch(
    detection: PIIDetection,
    patch: BehaviorPatch,
    *,
    checksum_backed: bool,
) -> bool:
    """Mirror edge_rules._checksum_blocks_rule for behavior patches."""
    if not checksum_backed or not patch.operations:
        return False
    from redibis.models import canonical_entity

    for op in patch.operations:
        if op.path != "verdict.entity":
            continue
        if op.value is None:
            # NOT_PII demotion clears entity — blocked when checksum protects current
            if detection.entity_type:
                return True
            continue
        new_ent = canonical_entity(str(op.value))
        cur_ent = canonical_entity(detection.entity_type or "")
        if new_ent != cur_ent:
            return True
    return False


def _evaluate_column(
    detection: PIIDetection,
    *,
    policy: BehaviorPolicy,
    compiled_rules: tuple,
    stage: HookStage,
    table: str,
    run_id: str,
    df: Any,
    registry,
    human_decisions: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> tuple[PIIDetection, BehaviorPatch, list]:
    from redibis.behavior.adapters.pii import (
        apply_patch_to_detection,
        build_pii_context,
    )
    from redibis.behavior.evaluator import PolicyEvaluator
    from redibis.behavior.models import RuleTraceStatus
    from redibis.behavior.reducer import PatchReducer

    ctx = build_pii_context(
        detection,
        run_id=run_id,
        table=table,
        stage=stage,
        registry_manifest_sha256=getattr(registry, "manifest_sha256", "") or "",
        df=df,
    )
    ctx = _enrich_facts_from_providers(ctx, registry)

    checksum_backed = any(
        ctx.facts.get(k) == "pass"
        for k in (
            "validators.luhn.status",
            "validators.iccid.status",
            "validators.nid.status",
        )
    )

    evaluator = PolicyEvaluator(registry=registry)
    reducer = PatchReducer(
        ctx.capabilities,
        human_decisions=_human_for_column(detection.column, human_decisions),
    )

    # Evaluate one rule at a time so checksum-blocked terminal matches fall
    # through to the next rule (parity with edge_rules.apply_edge_rules).
    all_traces: list = []
    current = detection
    applied_total = BehaviorPatch()
    matched_rule_id = ""

    remaining = list(compiled_rules)
    while remaining:
        rule = remaining[0]
        remaining = remaining[1:]
        traces, proposed = evaluator.evaluate((rule,), ctx, stage=stage)
        all_traces.extend(traces)
        matched = any(
            t.status == RuleTraceStatus.MATCHED
            or getattr(t.status, "value", "") == "matched"
            for t in traces
        )
        if not matched:
            continue

        reduced, _rejected, _blocked = reducer.reduce(proposed)
        if _checksum_blocks_patch(current, reduced, checksum_backed=checksum_backed):
            # Fall through — same as edge_rules continue after checksum block
            continue

        current = apply_patch_to_detection(
            current,
            reduced,
            rule_id=rule.id,
            policy_id=policy.id,
            checksum_backed=checksum_backed,
        )
        applied_total = reduced
        matched_rule_id = rule.id
        if rule.terminal:
            # Mark remaining as terminated in the trace for audit parity
            for leftover in remaining:
                from redibis.behavior.models import RuleTrace

                all_traces.append(
                    RuleTrace(
                        rule_id=leftover.id,
                        policy_id=leftover.policy_id,
                        priority=leftover.priority,
                        status=RuleTraceStatus.TERMINATED,
                        reason=leftover.reason,
                    )
                )
            break

        # Non-terminal: refresh context facts from updated detection for next rules
        ctx = build_pii_context(
            current,
            run_id=run_id,
            table=table,
            stage=stage,
            registry_manifest_sha256=getattr(registry, "manifest_sha256", "") or "",
            df=df,
        )
        ctx = _enrich_facts_from_providers(ctx, registry)

    return current, applied_total, all_traces


def apply_pre_verdict_thresholds(
    evidence: List[PIIDetection],
    thresholds: Any,
    *,
    policy: Optional[BehaviorPolicy] = None,
    policies: Optional[Sequence[BehaviorPolicy]] = None,
    df: Any = None,
    table: str = "",
    run_id: str = "",
    behavior_config: Optional[BehaviorConfig] = None,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> ThresholdApplyResult:
    """
    Evaluate pre_verdict threshold policies and return run-scoped Thresholds.

    Shadow mode: compute adjusted thresholds for audit/warnings but return the
    original thresholds (no apply). Active mode: return the adjusted copy.
    Disabled: return thresholds unchanged.
    """
    cfg = behavior_config or _load_behavior_config()
    mode = cfg.effective_mode("pii")
    policy_list = list(policies or [])
    if policy is not None:
        policy_list = [policy] + policy_list
    if mode is RuntimeMode.DISABLED or not policy_list:
        return ThresholdApplyResult(thresholds=thresholds, baseline_thresholds=thresholds)

    from redibis.behavior.adapters.pii import apply_threshold_patch, build_pii_context
    from redibis.behavior.compiler import compile_rules_for_evaluation
    from redibis.behavior.evaluator import PolicyEvaluator, sort_rules
    from redibis.behavior.reducer import PatchReducer

    registry = _resolve_registry(cfg)
    warnings: list[BehaviorPolicyWarning] = []
    adjusted = thresholds
    applied_any = False
    fail_mode = cfg.effective_fail_mode()

    for pol in policy_list:
        if pol.stage is not HookStage.PRE_VERDICT:
            continue
        compiled = sort_rules(compile_rules_for_evaluation(pol))
        for d in evidence:
            try:
                ctx = build_pii_context(
                    d,
                    run_id=run_id,
                    table=table,
                    stage=HookStage.PRE_VERDICT,
                    registry_manifest_sha256=getattr(registry, "manifest_sha256", "") or "",
                    df=df,
                )
                ctx = _enrich_facts_from_providers(ctx, registry)
                evaluator = PolicyEvaluator(registry=registry)
                _traces, proposed = evaluator.evaluate(
                    compiled, ctx, stage=HookStage.PRE_VERDICT
                )
                reducer = PatchReducer(ctx.capabilities)
                reduced, _rej, _blocked = reducer.reduce(proposed)
                if reduced.operations:
                    next_thr = apply_threshold_patch(adjusted, reduced)
                    if next_thr is not adjusted:
                        applied_any = True
                        adjusted = next_thr
            except Exception as exc:
                warn = BehaviorPolicyWarning(
                    policy_id=pol.id,
                    rule_id=None,
                    engine="pii",
                    stage=HookStage.PRE_VERDICT.value,
                    target=d.column,
                    fallback="baseline",
                    error=f"pre_verdict threshold eval failed: {exc}",
                )
                warnings.append(warn)
                if progress_callback:
                    progress_callback(f"⚠ Behavior pre_verdict fallback {d.column}: {exc}")
                if fail_mode is FailMode.FAIL_RUN:
                    raise

    if mode is RuntimeMode.SHADOW:
        if applied_any and progress_callback:
            progress_callback(
                "⚠ Behavior shadow: pre_verdict thresholds would change "
                "(not applied in shadow mode)"
            )
        return ThresholdApplyResult(
            thresholds=thresholds,
            warnings=warnings,
            applied=False,
            baseline_thresholds=thresholds,
        )

    return ThresholdApplyResult(
        thresholds=adjusted,
        warnings=warnings,
        applied=applied_any,
        baseline_thresholds=thresholds,
    )


def apply_pii_behavior_policies(
    detections: List[PIIDetection],
    *,
    df: Any = None,
    table: str = "",
    run_id: str = "",
    policy_pack: str = "telecom",
    edge_rules_overlay: Optional[list] = None,
    behavior_config: Optional[BehaviorConfig] = None,
    progress_callback: Optional[Callable[[str], None]] = None,
    baseline: Optional[List[PIIDetection]] = None,
    post_verdict_policy: Optional[BehaviorPolicy] = None,
    post_verdict_policies: Optional[Sequence[BehaviorPolicy]] = None,
    pii_decisions: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> BehaviorApplyResult:
    """
    Apply or shadow behavior policies for PII detections.

    Returns detections unchanged when behavior is disabled.

    ``detections`` must be equation (pre-edge) outcomes so shadow/active share
    the same input as the legacy edge-rule evaluator.
    """
    cfg = behavior_config or _load_behavior_config()
    mode = cfg.effective_mode("pii")
    if mode is RuntimeMode.DISABLED:
        return BehaviorApplyResult(detections=list(detections))

    from redibis.behavior.compiler import compile_rules_for_evaluation
    from redibis.behavior.evaluator import sort_rules

    policies: list[BehaviorPolicy] = list(post_verdict_policies or [])
    if post_verdict_policy is not None:
        policies = [post_verdict_policy] + policies
    if not policies:
        policies = resolve_policies_for_stage(
            cfg,
            engine="pii",
            stage=HookStage.POST_VERDICT,
            policy_pack=policy_pack,
            edge_rules_overlay=edge_rules_overlay,
        )
    if not policies:
        return BehaviorApplyResult(detections=list(detections))

    decisions = pii_decisions
    if decisions is None:
        decisions = _try_load_pii_decisions(table)

    registry = _resolve_registry(cfg)
    warnings: list[BehaviorPolicyWarning] = []
    out: List[PIIDetection] = list(detections)
    mismatches = 0
    fail_mode = cfg.effective_fail_mode()

    baseline_by_col = {d.column: d for d in (baseline or [])}

    for pol in policies:
        compiled = sort_rules(compile_rules_for_evaluation(pol))
        next_out: List[PIIDetection] = []
        for d in out:
            try:
                updated, _applied, _traces = _evaluate_column(
                    d,
                    policy=pol,
                    compiled_rules=compiled,
                    stage=HookStage.POST_VERDICT,
                    table=table,
                    run_id=run_id,
                    df=df,
                    registry=registry,
                    human_decisions=decisions,
                )
            except Exception as exc:
                warn = BehaviorPolicyWarning(
                    policy_id=pol.id,
                    rule_id=None,
                    engine="pii",
                    stage=HookStage.POST_VERDICT.value,
                    target=d.column,
                    fallback="baseline",
                    error=f"behavior evaluator failed: {exc}",
                )
                warnings.append(warn)
                msg = f"⚠ Behavior policy fallback for column={d.column}: {exc}"
                log.warning(msg)
                if progress_callback:
                    progress_callback(msg)
                if fail_mode is FailMode.FAIL_RUN:
                    raise
                next_out.append(d)
                continue
            next_out.append(updated)
        out = next_out

    final: List[PIIDetection] = []
    for d_orig, d_new in zip(detections, out):
        if mode is RuntimeMode.SHADOW:
            base = baseline_by_col.get(d_orig.column, d_orig)
            if (base.detected, base.entity_type) != (d_new.detected, d_new.entity_type):
                mismatches += 1
                warnings.append(
                    BehaviorPolicyWarning(
                        policy_id=policies[0].id,
                        rule_id=None,
                        engine="pii",
                        stage=HookStage.POST_VERDICT.value,
                        target=d_orig.column,
                        fallback="shadow_no_apply",
                        error=(
                            f"shadow mismatch: baseline="
                            f"({base.detected},{base.entity_type}) "
                            f"behavior=({d_new.detected},{d_new.entity_type})"
                        ),
                    )
                )
            final.append(base if d_orig.column in baseline_by_col else d_orig)
        else:
            final.append(d_new)

    if mismatches and progress_callback:
        progress_callback(
            f"⚠ Behavior shadow: {mismatches} column mismatch(es) vs edge rules"
        )

    return BehaviorApplyResult(
        detections=final,
        warnings=warnings,
        shadow_mismatches=mismatches,
    )


def behavior_content_fingerprint(policy: BehaviorPolicy) -> str:
    """Short fingerprint for audit linkage."""
    return hashlib.sha256(policy.content_sha256.encode()).hexdigest()[:16]
