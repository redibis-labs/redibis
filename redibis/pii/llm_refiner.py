"""Optional LLM refinement for ambiguous PII detections — RAI-guarded, shape-only prompts."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any, Optional

from redibis.memory.redaction import memory_safe_shape
from redibis.models import PIIDetection

_AMBIGUOUS_LOW = 0.4
_AMBIGUOUS_HIGH = 0.7

_REFINER_SYSTEM = """\
You are a PII classification assistant for data-governance review.

You receive column metadata and token-SHAPE masks only — never raw values.
Respond with a single JSON object:
{"verdict":"PII"|"NOT_PII"|"UNCERTAIN","confidence":0.0-1.0,"entity_type":"TYPE_OR_UNKNOWN","reasoning":"..."}
"""


def needs_llm_refinement(detection: PIIDetection) -> bool:
    """True when scores sit in the ambiguity band or engines disagree."""
    pres = detection.presidio_score
    ner = detection.gliner_score
    in_band = lambda s: s is not None and _AMBIGUOUS_LOW <= s <= _AMBIGUOUS_HIGH

    if in_band(pres) or in_band(ner):
        return True

    if pres is not None and ner is not None:
        pres_pii = pres >= _AMBIGUOUS_HIGH
        ner_pii = ner >= _AMBIGUOUS_HIGH
        if pres_pii != ner_pii:
            return True
    return False


def refine_detections(
    detections: list[PIIDetection],
    thresholds: "Thresholds",
    *,
    redibis_config: Any = None,
    provider: Any = None,
) -> list[PIIDetection]:
    """Refine ambiguous columns via LLM when ``pii.llm.enabled`` is set."""
    cfg = redibis_config
    llm_enabled = bool(getattr(getattr(cfg, "pii", None), "llm", None) and cfg.pii.llm.enabled)
    if not llm_enabled and provider is None:
        return detections

    out: list[PIIDetection] = []
    for det in detections:
        if not needs_llm_refinement(det):
            out.append(det)
            continue
        result = refine_with_llm(
            column=det.column,
            sample_values=[],
            current_entity=det.entity_type or "UNKNOWN",
            current_confidence=max(
                det.presidio_score or 0.0,
                det.gliner_score or 0.0,
            ),
            presidio_score=det.presidio_score,
            gliner_score=det.gliner_score,
            provider=provider,
            redibis_config=cfg,
        )
        updated = deepcopy(det)
        if result.get("error"):
            out.append(updated)
            continue
        updated.llm_score = float(result.get("confidence") or 0.0)
        updated.llm_verdict = _map_verdict(result.get("verdict"))
        updated.llm_reasoning = str(result.get("reasoning") or "")
        if result.get("entity_type") and result.get("entity_type") != "UNKNOWN":
            updated.entity_type = str(result.get("entity_type"))
        out.append(updated)
    return out


def refine_with_llm(
    *,
    column: str,
    sample_values: list[str],
    current_entity: str,
    current_confidence: float,
    presidio_score: Optional[float] = None,
    gliner_score: Optional[float] = None,
    provider: Any = None,
    redibis_config: Any = None,
) -> dict[str, Any]:
    """
    Resolve one ambiguous column via LLM. Sample values are redacted to shapes
    before any model call. Routes through ``guarded_model_call``.
    """
    cfg = redibis_config
    llm_cfg = getattr(getattr(cfg, "pii", None), "llm", None) if cfg else None
    if provider is None:
        try:
            from redibis.config import load_global_settings_optional
            from redibis.enrich.capability_routing import (
                build_bindings_from_global,
                resolve_model_binding,
            )
            from redibis.enrich.providers import get_provider

            bindings = build_bindings_from_global(
                load_global_settings_optional(),
                agents_cfg=getattr(cfg, "agents", None),
            )
            binding = resolve_model_binding("pii.refiner", bindings=bindings)
            if binding.enabled and binding.provider:
                provider = get_provider(
                    binding.provider,
                    model=binding.model or "",
                    api_key=(getattr(llm_cfg, "api_key", None) or None) if llm_cfg else None,
                    endpoint_url=(getattr(llm_cfg, "endpoint_url", None) or None) if llm_cfg else None,
                )
        except Exception:
            provider = None
    if provider is None:
        if not llm_cfg or not llm_cfg.enabled:
            return {"verdict": "SKIP", "error": "pii.llm.enabled is false"}
        try:
            from redibis.enrich.providers import get_provider

            provider = get_provider(
                llm_cfg.provider,
                model=llm_cfg.model_name or "",
                api_key=llm_cfg.api_key or None,
                endpoint_url=llm_cfg.endpoint_url,
            )
        except Exception as exc:
            return {"verdict": "SKIP", "error": str(exc)}

    shapes = [memory_safe_shape(v) for v in sample_values[:8]]
    user_prompt = json.dumps({
        "column": column,
        "current_entity": current_entity,
        "current_confidence": current_confidence,
        "presidio_score": presidio_score,
        "gliner_score": gliner_score,
        "sample_shapes": shapes,
    }, indent=2)

    from redibis.config import RAIConfig, RedibisConfig
    from redibis.telemetry.model_gateway import guarded_model_call

    rai_cfg = cfg.rai if isinstance(cfg, RedibisConfig) else RAIConfig()
    residency = getattr(provider, "residency", "") or "local"

    try:
        raw, rai_report = guarded_model_call(
            lambda: provider.complete(_REFINER_SYSTEM, user_prompt, json_mode=True),
            model_id=getattr(provider, "name", None) or getattr(provider, "model", "pii-llm"),
            provider=provider,
            user_prompt=user_prompt,
            system_prompt=_REFINER_SYSTEM,
            residency=residency,
            rai_config=rai_cfg,
            redibis_config=cfg if isinstance(cfg, RedibisConfig) else None,
            model_role="pii.refiner",
        )
    except PermissionError as exc:
        return {"verdict": "BLOCKED", "error": str(exc)}

    parsed = _parse_refiner_json(raw)
    if rai_report:
        parsed["rai"] = rai_report
    return parsed


def _map_verdict(verdict: Any) -> str:
    v = str(verdict or "").upper()
    if v == "PII":
        return "CONFIRMED"
    if v in ("NOT_PII", "NOT PII", "CLEAN"):
        return "REJECTED"
    return "UNCERTAIN"


def _parse_refiner_json(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {"verdict": "UNCERTAIN", "confidence": 0.0, "reasoning": "invalid JSON from model"}
    if not isinstance(data, dict):
        return {"verdict": "UNCERTAIN", "confidence": 0.0, "reasoning": "non-object JSON"}
    return {
        "verdict": data.get("verdict", "UNCERTAIN"),
        "confidence": float(data.get("confidence") or 0.0),
        "entity_type": data.get("entity_type") or "UNKNOWN",
        "reasoning": str(data.get("reasoning") or ""),
    }
