"""Deterministic classification reasoning trace for LLM enrichment context."""

from __future__ import annotations

from typing import Any, Optional

from redibis.classification.models import ClassificationResult
from redibis.classification.policy_pack import ClassificationPolicy
from redibis.classification.service import ClassificationService
from redibis.contracts.privacy import col_pii_engine

_RESULT_TAG_DOMAINS = frozenset({
    "DataSensitivity",
    "TelcoDataType",
    "RetentionPolicy",
    "DataQuality",
    "Lifecycle",
})


def build_deterministic_reasoning(
    prop: dict,
    telemetry: dict,
    policy: ClassificationPolicy,
    table: str,
    *,
    run_detection: Optional[dict] = None,
) -> dict[str, Any]:
    """Build per-column policy-engine trace + result for the enrichment LLM."""
    ce = col_pii_engine(prop) if prop else {}
    entity_type = (
        ce.get("entity_type")
        or telemetry.get("entity_type")
        or prop.get("entity_type")
    )

    contract = _single_column_contract(table, prop)
    svc = ClassificationService(policy)
    result = svc.classify_column(contract, table, str(prop.get("name") or ""))

    why = _engine_why_lines(telemetry, run_detection, entity_type)
    why.extend(_pack_why_lines(policy, entity_type, result))

    out: dict[str, Any] = {
        "why": why,
        "result": _reasoning_result(prop, result),
    }
    if entity_type:
        out["entity_type"] = entity_type
    return out


def _single_column_contract(table: str, prop: dict) -> dict:
    return {
        "schema": [{
            "physicalName": table,
            "properties": [prop],
        }],
    }


def _engine_why_lines(
    telemetry: dict,
    run_detection: Optional[dict],
    entity_type: Any,
) -> list[str]:
    det = run_detection or {}
    why: list[str] = []
    regex_hits = det.get("regex_hits") or telemetry.get("regex_hits") or []
    decision_rule = (
        telemetry.get("decision_rule")
        or det.get("decision_rule")
        or (telemetry.get("edge_rule"))
    )
    et = entity_type or det.get("entity_type") or telemetry.get("entity_type")

    for hit in regex_hits[:3]:
        if not isinstance(hit, dict):
            continue
        pattern = hit.get("pattern_name") or "unknown"
        match_rate = hit.get("match_rate")
        hit_entity = hit.get("entity_type") or et or "UNKNOWN"
        if match_rate is not None:
            pct = int(round(float(match_rate) * 100))
            rule_suffix = f" (decision_rule: {decision_rule})" if decision_rule else ""
            why.append(
                f"engine: regex {pattern} matched {pct}% → {hit_entity}{rule_suffix}"
            )
        elif hit.get("score") is not None:
            why.append(
                f"engine: regex {pattern} score {hit['score']} → {hit_entity}"
                + (f" (decision_rule: {decision_rule})" if decision_rule else "")
            )

    if not why and et and decision_rule:
        why.append(f"engine: {et} (decision_rule: {decision_rule})")
    elif not why and et:
        conf = telemetry.get("confidence") or det.get("confidence")
        if conf is not None:
            why.append(f"engine: {et} confidence {conf}")
        else:
            why.append(f"engine: detected {et}")

    ner_hits = det.get("ner_hits") or telemetry.get("ner_hits") or []
    for hit in ner_hits[:2]:
        if not isinstance(hit, dict):
            continue
        model = hit.get("model") or hit.get("engine") or "ner"
        label = hit.get("entity_type") or hit.get("label") or et
        score = hit.get("score")
        if label:
            line = f"engine: {model} → {label}"
            if score is not None:
                line += f" (score {score})"
            why.append(line)

    return why


def _pack_why_lines(
    policy: ClassificationPolicy,
    entity_type: Any,
    result: ClassificationResult,
) -> list[str]:
    why: list[str] = []
    if entity_type:
        mapping = policy.entity_type_mapping.get(str(entity_type).upper())
        if mapping:
            why.append(
                f"pack: {entity_type} → {mapping['domain']}:{mapping['tag']} "
                "(entity_type_mapping)"
            )

    for tag in result.resolved_tags:
        if not tag.derived:
            continue
        if tag.source.startswith("cotag:"):
            trigger = tag.source.split(":", 1)[1]
            why.append(
                f"pack: {trigger} mandates {tag.domain}:{tag.tag} (cotag_matrix)"
            )
        elif tag.source == "security_derivation":
            driver = _security_driver_tag(result, policy, tag.tag)
            if driver:
                why.append(f"pack: {driver} → {tag.tag} (security_derivation)")
            else:
                why.append(f"pack: → {tag.domain}:{tag.tag} (security_derivation)")

    return why


def _security_driver_tag(
    result: ClassificationResult,
    policy: ClassificationPolicy,
    security_tag: str,
) -> str:
    best_level = -1
    best_driver = ""
    for tag in result.resolved_tags:
        if tag.domain != "DataSensitivity":
            continue
        derived = policy.security_derivation.get(tag.tag)
        if derived != security_tag:
            continue
        level = policy.security_level(derived)
        if level > best_level:
            best_level = level
            best_driver = tag.tag
    return best_driver


def _reasoning_result(prop: dict, result: ClassificationResult) -> dict[str, Any]:
    tags: list[str] = []
    security: Optional[str] = None
    for resolved in result.resolved_tags:
        if resolved.domain == "DataSecurity":
            security = resolved.tag
        elif resolved.domain in _RESULT_TAG_DOMAINS:
            if resolved.tag not in tags:
                tags.append(resolved.tag)

    out: dict[str, Any] = {"tags": tags}
    classification = prop.get("classification")
    if classification:
        out["classification"] = classification
    if security:
        out["security"] = security
    return out
