"""
redibis.behavior.adapters.pii
===============================
PII engine behavior adapter.

Responsibilities
----------------
1. Convert existing EdgeRuleColumnContext facts → BehaviorContext facts.
2. Convert ClassificationPolicy.edge_rules → BehaviorPolicy (compatibility).
3. Declare PII-specific capabilities (which facts/ops/patches are allowed).
4. Apply a reduced BehaviorPatch to a PIIDetection (immutably).
5. Consult PiiDecisionStore at scan time so durable overlays block policy.
6. Preserve detector/equation invariant: does not set detected=True from
   within detect_pii(); only post-verdict corrections are authoritative.

Integration point
-----------------
Called by pipeline.run_pii_detection when behavior.enabled=True and
behavior.mode in (shadow, active).

Invariants
----------
- Never sets detected=True inside detect_pii().
- Never writes to the contracts bucket.
- Patch application is immutable (dataclasses.replace).
- Raw cell values are never present in BehaviorContext.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from typing import Any, Mapping, Optional

from redibis.behavior.models import (
    Authority,
    BehaviorCapabilities,
    BehaviorContext,
    BehaviorPatch,
    BehaviorPolicySet,
    BehaviorPolicyWarning,
    EvaluationTrace,
    FailMode,
    HookStage,
    JSONValue,
    PatchOperation,
    RuntimeMode,
    RuleTraceStatus,
)
from redibis.models import PIIDetection

# Bounded threshold adjustment limits — adapter-owned, not policy-authored
_THRESHOLD_HARD_MIN = 0.30
_THRESHOLD_HARD_MAX = 0.99


# ---------------------------------------------------------------------------
# Fact extraction
# ---------------------------------------------------------------------------

def build_pii_facts(
    detection: PIIDetection,
    column_profile: Any = None,
    *,
    df: Any = None,
    checksums: Optional[Mapping[str, str]] = None,
    min_value: Optional[float] = None,
    max_value: Optional[float] = None,
) -> dict[str, JSONValue]:
    """
    Build a privacy-safe fact map from a PIIDetection.

    No raw cell values are included.
    Profile metrics are bound from the profile object if available.
    Checksum statuses and numeric ranges may be supplied precomputed; when
    ``df`` is given and they are missing, aggregate helpers may fill them once.
    """
    facts: dict[str, JSONValue] = {
        "column.name":         detection.column,
        "verdict.detected":    bool(detection.detected),
        "verdict.entity":      detection.entity_type,
        "verdict.confidence":  float(detection.confidence or 0.0),
        "verdict.equation":    detection.equation_used or "independent",
    }

    # Evidence scores (already computed by detect_pii — never recomputed here)
    if detection.presidio_score is not None:
        facts["evidence.regex.score"] = float(detection.presidio_score)
    if detection.presidio_match_rate is not None:
        facts["evidence.regex.match_rate"] = float(detection.presidio_match_rate)
    if detection.gliner_score is not None:
        facts["evidence.ner.score"] = float(detection.gliner_score)
    if detection.gliner_match_rate is not None:
        facts["evidence.ner.match_rate"] = float(detection.gliner_match_rate)

    # Phone validator outcomes
    if detection.phone_valid_rate is not None:
        facts["validators.phone.valid_rate"] = float(detection.phone_valid_rate)
    if detection.msisdn_valid_rate is not None:
        facts["validators.msisdn.valid_rate"] = float(detection.msisdn_valid_rate)

    # Profile metrics from column_profile if present
    if column_profile is not None:
        null_rate = getattr(column_profile, "null_rate", None)
        if null_rate is not None:
            facts["profile.null_rate"] = float(null_rate)
        cardinality = getattr(column_profile, "cardinality_ratio", None)
        if cardinality is not None:
            facts["profile.cardinality_ratio"] = float(cardinality)
        logical_type = getattr(column_profile, "logical_type", None)
        if logical_type:
            facts["column.logical_type"] = str(logical_type)
            lt = str(logical_type).lower()
            facts["column.is_numeric"] = lt in (
                "integer", "float", "number", "double", "decimal"
            )
            facts["column.is_integer"] = lt == "integer"

    if min_value is not None:
        facts["column.min_value"] = float(min_value)
    if max_value is not None:
        facts["column.max_value"] = float(max_value)

    resolved_checksums = dict(checksums or {})
    if not resolved_checksums and df is not None:
        resolved_checksums, range_min, range_max = _aggregate_from_frame(
            detection.column, df
        )
        if "column.min_value" not in facts and range_min is not None:
            facts["column.min_value"] = range_min
            facts["column.is_numeric"] = True
        if "column.max_value" not in facts and range_max is not None:
            facts["column.max_value"] = range_max
            facts["column.is_numeric"] = True

    for key, status in resolved_checksums.items():
        if key in ("luhn_checksum", "validators.luhn.status"):
            facts["validators.luhn.status"] = str(status)
        elif key in ("iccid_checksum", "validators.iccid.status"):
            facts["validators.iccid.status"] = str(status)
        elif key in ("nid_checksum", "validators.nid.status"):
            facts["validators.nid.status"] = str(status)

    if detection.phone_regions:
        facts["validators.phone.regions"] = list(detection.phone_regions.keys())

    if detection.presidio_pattern:
        facts["evidence.context_hit"] = True
    elif detection.triage_score and detection.triage_score >= 0.5:
        facts["evidence.context_hit"] = True
    else:
        facts["evidence.context_hit"] = False

    return facts


def _aggregate_from_frame(
    column: str, df: Any
) -> tuple[dict[str, str], Optional[float], Optional[float]]:
    """Compute checksum statuses and numeric range without exposing cell values."""
    checksums: dict[str, str] = {}
    min_val: Optional[float] = None
    max_val: Optional[float] = None
    try:
        import pandas as pd
        from redibis.pii.telecom_signals import checksum_pass_rates

        if column not in df.columns:
            return checksums, min_val, max_val
        series = df[column]
        if pd.api.types.is_numeric_dtype(series):
            non_null = pd.to_numeric(series, errors="coerce").dropna()
            if len(non_null):
                min_val = float(non_null.min())
                max_val = float(non_null.max())
        str_values = [str(v) for v in series.dropna().head(500).tolist()]
        rates = checksum_pass_rates(str_values)
        luhn_tested = any(len(re.sub(r"\D", "", str(v))) == 15 for v in str_values if v)
        iccid_tested = any(
            re.sub(r"\D", "", str(v)).startswith("89")
            and 17 <= len(re.sub(r"\D", "", str(v))) <= 20
            for v in str_values
            if v
        )
        nid_tested = any(len(re.sub(r"\D", "", str(v))) == 14 for v in str_values if v)
        checksums["luhn_checksum"] = (
            "pass" if luhn_tested and rates.get("luhn_pass_rate", 0.0) >= 0.5 else "fail"
        )
        checksums["iccid_checksum"] = (
            "pass" if iccid_tested and rates.get("iccid_pass_rate", 0.0) >= 0.5 else "fail"
        )
        checksums["nid_checksum"] = (
            "pass"
            if nid_tested and rates.get("egypt_nid_pass_rate", 0.0) >= 0.5
            else "fail"
        )
    except Exception:
        return checksums, min_val, max_val
    return checksums, min_val, max_val


def build_pii_context(
    detection: PIIDetection,
    *,
    run_id: str = "",
    table: str = "",
    stage: HookStage = HookStage.POST_VERDICT,
    column_profile: Any = None,
    registry_manifest_sha256: str = "",
    environment: Optional[str] = None,
    jurisdiction: Optional[str] = None,
    df: Any = None,
    checksums: Optional[Mapping[str, str]] = None,
) -> BehaviorContext:
    """Build a BehaviorContext for PII evaluation."""
    facts = build_pii_facts(
        detection,
        column_profile=column_profile,
        df=df,
        checksums=checksums,
    )
    caps = _pii_capabilities(stage)
    return BehaviorContext(
        run_id=run_id,
        table=table,
        column=detection.column,
        engine="pii",
        stage=stage,
        facts=facts,
        capabilities=caps,
        registry_manifest_sha256=registry_manifest_sha256,
        environment=environment,
        jurisdiction=jurisdiction,
    )


# ---------------------------------------------------------------------------
# Capabilities declaration
# ---------------------------------------------------------------------------

def _pii_capabilities(stage: HookStage) -> BehaviorCapabilities:
    """Return adapter capabilities for the given PII hook stage."""
    if stage == HookStage.POST_VERDICT:
        return BehaviorCapabilities(
            readable_facts=frozenset({
                "column.name", "column.logical_type",
                "column.is_numeric", "column.is_integer",
                "column.min_value", "column.max_value",
                "profile.null_rate", "profile.cardinality_ratio",
                "evidence.regex.score", "evidence.regex.match_rate",
                "evidence.ner.score", "evidence.ner.match_rate",
                "validators.phone.valid_rate", "validators.msisdn.valid_rate",
                "validators.phone.regions",
                "validators.luhn.status", "validators.iccid.status",
                "validators.nid.status",
                "evidence.context_hit",
                "verdict.detected", "verdict.entity",
                "verdict.confidence", "verdict.equation",
            }),
            allowed_actions=frozenset({
                "core.verdict.set_entity",
                "core.verdict.set_detected",
                "core.verdict.set_confidence",
                "core.review.require",
                "core.review.add_reason",
            }),
            allowed_patch_operations=frozenset({
                "verdict.detected",
                "verdict.entity",
                "verdict.confidence",
                "review.require",
                "review.add_reason",
            }),
            immutable_paths=frozenset(),
        )
    if stage == HookStage.PRE_VERDICT:
        return BehaviorCapabilities(
            readable_facts=frozenset({
                "column.name", "column.logical_type",
                "column.is_numeric", "column.is_integer",
                "profile.null_rate", "profile.cardinality_ratio",
                "evidence.regex.score", "evidence.regex.match_rate",
                "evidence.ner.score", "evidence.ner.match_rate",
                "validators.phone.valid_rate",
            }),
            allowed_actions=frozenset({
                "core.threshold.set_for_run",
                "core.review.require",
            }),
            allowed_patch_operations=frozenset({
                "threshold.presidio_min",
                "threshold.gliner_min",
                "threshold.phone_min",
                "review.require",
            }),
            immutable_paths=frozenset(),
            value_constraints={
                "threshold.presidio_min": {
                    "minimum": _THRESHOLD_HARD_MIN,
                    "maximum": _THRESHOLD_HARD_MAX,
                },
                "threshold.gliner_min": {
                    "minimum": _THRESHOLD_HARD_MIN,
                    "maximum": _THRESHOLD_HARD_MAX,
                },
                "threshold.phone_min": {
                    "minimum": _THRESHOLD_HARD_MIN,
                    "maximum": _THRESHOLD_HARD_MAX,
                },
            },
        )
    # Default: no operations allowed (unknown stage)
    return BehaviorCapabilities()


# ---------------------------------------------------------------------------
# Edge-rule compatibility converter
# ---------------------------------------------------------------------------

def edge_rules_to_behavior_policy(
    policy: "ClassificationPolicy",  # type: ignore[name-defined]
    *,
    policy_id: str = "edge_rules_compat",
    version: str = "1.0.0",
) -> Optional["BehaviorPolicy"]:  # type: ignore[name-defined]
    """
    Convert ClassificationPolicy.edge_rules into a v1 BehaviorPolicy.

    This compatibility path preserves current rule IDs, order, checksum
    protection, and decision_path semantics.

    Returns None if there are no edge rules.
    """
    from redibis.behavior.models import (
        AuthoredRule, BehaviorPolicy, ConditionGroup, ConditionNode,
        EffectCall, PolicySource, Predicate, RuleScope,
    )
    from redibis.behavior.compiler import compile_rules_for_evaluation
    import hashlib, json

    if not policy.edge_rules:
        return None

    rules: list[AuthoredRule] = []
    for priority, rule in enumerate(reversed(policy.edge_rules), start=100):
        rid = str(rule.get("id") or f"rule_{priority}")
        when = rule.get("when") or {}
        then = rule.get("then") or {}

        # Build condition from 'when' keys
        predicates: list[ConditionNode] = []
        for k, v in when.items():
            if k == "name_matches":
                predicates.append(Predicate(fact="column.name", operator="matches", expected=str(v)))
            elif k == "entity":
                predicates.append(Predicate(fact="verdict.entity", operator="eq", expected=str(v)))
            elif k == "is_numeric":
                predicates.append(Predicate(fact="column.is_numeric", operator="is_true" if v else "is_false", expected=None))
            elif k == "is_integer":
                predicates.append(Predicate(fact="column.is_integer", operator="is_true" if v else "is_false", expected=None))
            elif k == "in_range":
                lo, hi = float(v[0]), float(v[1])
                predicates.append(Predicate(
                    fact="column.min_value", operator="gte", expected=lo
                ))
                predicates.append(Predicate(
                    fact="column.max_value", operator="lte", expected=hi
                ))
            elif k in ("cardinality_ratio_gte",):
                predicates.append(Predicate(fact="profile.cardinality_ratio", operator="gte", expected=float(v)))
            elif k in ("cardinality_ratio_lte",):
                predicates.append(Predicate(fact="profile.cardinality_ratio", operator="lte", expected=float(v)))
            elif k == "null_rate_lte":
                predicates.append(Predicate(fact="profile.null_rate", operator="lte", expected=float(v)))
            elif k == "valid_rate_gte":
                predicates.append(Predicate(fact="validators.phone.valid_rate", operator="gte", expected=float(v)))
            elif k == "context_hit":
                predicates.append(Predicate(
                    fact="evidence.context_hit",
                    operator="is_true" if v else "is_false",
                    expected=None,
                ))
            elif k == "logical_type":
                predicates.append(Predicate(
                    fact="column.logical_type", operator="eq", expected=str(v)
                ))
            elif k in ("luhn_checksum", "iccid_checksum", "nid_checksum"):
                fact_id = {
                    "luhn_checksum": "validators.luhn.status",
                    "iccid_checksum": "validators.iccid.status",
                    "nid_checksum": "validators.nid.status",
                }[k]
                predicates.append(Predicate(fact=fact_id, operator="eq", expected=str(v)))
            elif k == "region_in":
                allowed = v if isinstance(v, list) else [v]
                predicates.append(Predicate(
                    fact="validators.phone.regions",
                    operator="contains_any",
                    expected=list(allowed),
                ))

        if len(predicates) == 1:
            condition: ConditionNode = predicates[0]
        elif predicates:
            condition = ConditionGroup(kind="all", children=tuple(predicates))
        else:
            # No predicates → always-true (match all) — use a dummy predicate
            condition = Predicate(fact="verdict.detected", operator="exists", expected=None)

        # Build effects from 'then' keys
        effects: list[EffectCall] = []
        se = then.get("set_entity")
        if se:
            if str(se) == "NOT_PII":
                effects.append(EffectCall(
                    effect="core.verdict.set_detected",
                    params={"detected": False},
                ))
                effects.append(EffectCall(
                    effect="core.verdict.set_entity",
                    params={"entity": None},
                ))
                effects.append(EffectCall(
                    effect="core.verdict.set_confidence",
                    params={"confidence": 0.0},
                ))
            else:
                effects.append(EffectCall(
                    effect="core.verdict.set_entity",
                    params={"entity": str(se)},
                ))
                effects.append(EffectCall(
                    effect="core.verdict.set_detected",
                    params={"detected": True},
                ))
        forbid = then.get("forbid_entity")
        if forbid:
            # Demote when current entity matches — expressed as set_detected False
            # with a condition already matching entity; keep as review+demote pair.
            effects.append(EffectCall(
                effect="core.verdict.set_detected",
                params={"detected": False},
            ))
            effects.append(EffectCall(
                effect="core.verdict.set_entity",
                params={"entity": None},
            ))
            effects.append(EffectCall(
                effect="core.verdict.set_confidence",
                params={"confidence": 0.0},
            ))
            # Scope the forbid with an entity predicate if not already present
            if not any(
                isinstance(p, Predicate) and p.fact == "verdict.entity"
                for p in predicates
            ):
                predicates.append(Predicate(
                    fact="verdict.entity", operator="eq", expected=str(forbid)
                ))
        if then.get("require_review"):
            effects.append(EffectCall(
                effect="core.review.require",
                params={"role": "steward"},
            ))
        note = then.get("note", "")
        if note:
            effects.append(EffectCall(
                effect="core.review.add_reason",
                params={"reason": str(note)},
            ))
        # Classification carry-forwards (consumed downstream like edge_tags)
        for tag in list(then.get("add_tags") or []):
            effects.append(EffectCall(
                effect="core.review.add_reason",
                params={"reason": f"edge_tag:{tag}"},
            ))
        if then.get("set_classification"):
            effects.append(EffectCall(
                effect="core.review.add_reason",
                params={"reason": f"edge_classification:{then['set_classification']}"},
            ))
        if then.get("set_masking_policy"):
            effects.append(EffectCall(
                effect="core.review.add_reason",
                params={"reason": f"edge_masking:{then['set_masking_policy']}"},
            ))

        # Rebuild condition if forbid_entity added a predicate after initial build
        if len(predicates) == 1:
            condition = predicates[0]
        elif predicates:
            condition = ConditionGroup(kind="all", children=tuple(predicates))
        else:
            condition = Predicate(fact="verdict.detected", operator="exists", expected=None)

        if not effects:
            # Nothing actionable — skip rule
            continue

        reason = note or f"Edge rule {rid}: {then}"
        rules.append(AuthoredRule(
            id=rid,
            when=condition,
            effects=tuple(effects),
            reason=reason[:512],
            priority=priority,
            terminal=True,   # edge rules are first-match-wins → terminal
        ))

    if not rules:
        return None

    # Compute a stable content SHA from the original edge_rules data
    raw_json = json.dumps(policy.edge_rules, sort_keys=True, separators=(",", ":"))
    content_sha256 = hashlib.sha256(raw_json.encode()).hexdigest()

    scope = RuleScope()
    return BehaviorPolicy(
        id=policy_id,
        version=version,
        description="Compatibility policy converted from ClassificationPolicy.edge_rules",
        engine="pii",
        stage=HookStage.POST_VERDICT,
        scope=scope,
        rules=tuple(rules),
        content_sha256=content_sha256,
        source=PolicySource.BUILTIN,
        owner="system",
    )


# ---------------------------------------------------------------------------
# Patch application
# ---------------------------------------------------------------------------

def apply_patch_to_detection(
    detection: PIIDetection,
    patch: BehaviorPatch,
    *,
    rule_id: str = "",
    policy_id: str = "",
    checksum_backed: bool = False,
) -> PIIDetection:
    """
    Apply a reduced BehaviorPatch to a PIIDetection immutably.

    Respects the detector/equation invariant:
    - detected is only set if the patch explicitly includes verdict.detected.
    - decision_path is appended with behavior-policy trace info.
    - Passing checksums (checksum_backed) block entity changes that differ from
      the current entity — same precedence as edge_rules._checksum_blocks_rule.
    """
    updates: dict[str, Any] = {}

    for op in patch.operations:
        if op.op != "set":
            continue   # only set operations in v1

        if op.path == "verdict.entity":
            if checksum_backed and op.value is not None:
                from redibis.models import canonical_entity
                new_ent = canonical_entity(str(op.value))
                cur_ent = canonical_entity(detection.entity_type or "")
                if new_ent != cur_ent:
                    # Validators outrank name-based entity overrides
                    continue
            if op.value is None:
                updates["entity_type"] = None
            else:
                from redibis.models import canonical_entity
                updates["entity_type"] = canonical_entity(str(op.value))

        elif op.path == "verdict.detected":
            if checksum_backed and op.value is False and detection.detected:
                # Demotion blocked when a checksum already protects the entity
                continue
            updates["detected"] = bool(op.value)

        elif op.path == "verdict.confidence":
            v = op.value
            if isinstance(v, (int, float)):
                updates["confidence"] = max(0.0, min(1.0, float(v)))

    if patch.require_review:
        updates["llm_verdict"] = "UNCERTAIN"

    # Edge-compat carry-forwards encoded as review reasons
    edge_tags = list(detection.edge_tags or [])
    for reason in patch.reasons or ():
        r = str(reason)
        if r.startswith("edge_tag:"):
            tag = r[len("edge_tag:"):]
            if tag and tag not in edge_tags:
                edge_tags.append(tag)
        elif r.startswith("edge_classification:"):
            updates["edge_classification"] = r[len("edge_classification:"):]
        elif r.startswith("edge_masking:"):
            updates["edge_masking_policy"] = r[len("edge_masking:"):]
    if edge_tags != list(detection.edge_tags or []):
        updates["edge_tags"] = edge_tags

    # Also scan review.add_reason ops (reasons may live on ops)
    for op in patch.operations:
        if op.path == "review.add_reason" and isinstance(op.value, str):
            r = op.value
            if r.startswith("edge_tag:"):
                tag = r[len("edge_tag:"):]
                edge_tags = list(updates.get("edge_tags") or edge_tags)
                if tag and tag not in edge_tags:
                    edge_tags.append(tag)
                    updates["edge_tags"] = edge_tags
            elif r.startswith("edge_classification:"):
                updates["edge_classification"] = r[len("edge_classification:"):]
            elif r.startswith("edge_masking:"):
                updates["edge_masking_policy"] = r[len("edge_masking:"):]

    if not updates and not patch.require_review:
        return detection

    # Align demotion confidence with edge_rules.refine_detection
    if updates.get("detected") is False and "confidence" not in updates:
        updates["confidence"] = 0.0

    # Build decision_path trace
    path = detection.decision_path or ""
    parts: list[str] = []
    if rule_id:
        parts.append(f"behavior_policy:{policy_id}:{rule_id}")
    if patch.reasons:
        parts.append("|".join(r[:128] for r in patch.reasons[:3]))
    if parts:
        trace = "; ".join(parts)
        updates["decision_path"] = f"{path}; {trace}".strip("; ") if path else trace

    return replace(detection, **updates)


# ---------------------------------------------------------------------------
# Threshold effect application
# ---------------------------------------------------------------------------

def human_authority_for_decision(
    decision: Optional[Mapping[str, Any]],
) -> dict[str, Authority]:
    """
    Map a PiiDecisionStore column decision to reducer human-authority locks.

    Both ``not_pii`` and ``pii`` overlays lock verdict entity/detected so an
    approved behavior policy cannot resurrect or demote against the overlay.
    """
    if not decision:
        return {}
    status = str(decision.get("status") or "")
    if status not in ("not_pii", "pii"):
        return {}
    return {
        "verdict.entity": Authority.HUMAN_DECISION,
        "verdict.detected": Authority.HUMAN_DECISION,
    }


def apply_threshold_patch(
    thresholds: Any,
    patch: BehaviorPatch,
) -> Any:
    """
    Apply threshold.set_for_run operations to a Thresholds object (run-scoped).

    Values are clamped to [_THRESHOLD_HARD_MIN, _THRESHOLD_HARD_MAX].
    Returns a new Thresholds object (not mutating global config).
    """
    from dataclasses import replace as dc_replace

    updates: dict[str, float] = {}
    for op in patch.operations:
        if not op.path.startswith("threshold."):
            continue
        threshold_name = op.path[len("threshold."):]
        if not isinstance(op.value, (int, float)):
            continue
        clamped = max(_THRESHOLD_HARD_MIN, min(_THRESHOLD_HARD_MAX, float(op.value)))
        if threshold_name == "presidio_min":
            updates["presidio_min"] = clamped
        elif threshold_name == "gliner_min":
            updates["gliner_min"] = clamped
        elif threshold_name == "phone_min":
            updates["phone_min"] = clamped

    if updates:
        return dc_replace(thresholds, **updates)
    return thresholds


def mark_threshold_promotions(
    baseline: list[PIIDetection],
    adjusted: list[PIIDetection],
    *,
    reason: str = "behavior pre_verdict threshold promotion",
) -> list[PIIDetection]:
    """
    Mark false-negative→true promotions caused by threshold adjustment for review.
    """
    by_col = {d.column: d for d in baseline}
    out: list[PIIDetection] = []
    for d in adjusted:
        base = by_col.get(d.column)
        if base is not None and (not base.detected) and d.detected:
            path = d.decision_path or ""
            trace = f"behavior_policy:pre_verdict:require_review|{reason}"
            out.append(replace(
                d,
                llm_verdict="UNCERTAIN",
                decision_path=f"{path}; {trace}".strip("; ") if path else trace,
            ))
        else:
            out.append(d)
    return out
