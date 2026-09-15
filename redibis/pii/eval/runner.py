"""Scan a validated dataset with TextPIIService and score the spans."""

from __future__ import annotations

import threading
from typing import Any, Callable, Mapping, Optional, Sequence

from redibis.pii.eval.normalize import PROFILE_V1_ID, load_profile
from redibis.pii.eval.provenance import base_provenance, collect_scan_inventory, new_run_uuid, rules_checksum
from redibis.pii.eval.span_metrics import (
    DEFAULT_TIERS,
    DatasetValidationError,
    current_redibis_version,
    evaluate_dataset,
    validate_dataset,
)

ProgressCb = Callable[[str, dict[str, Any]], None]


class EvalCancelled(Exception):
    """Raised when a caller stops an in-flight evaluation."""


def eval_limiter_weight(
    case_count: int, total_chars: int, *, use_llm: bool = False
) -> int:
    """How many sliding-window slots one evaluation should consume."""
    weight = 1 + max(0, int(case_count)) // 10 + max(0, int(total_chars)) // 20_000
    if use_llm:
        weight += 5
    return max(1, weight)


def _scan_case(svc: Any, case: Mapping[str, Any], options: Mapping[str, Any]) -> Any:
    kwargs: dict[str, Any] = {
        "language": case.get("language") or options.get("language") or "en",
        "engines": options.get("engines") or "both",
        "min_score": float(options.get("min_score") or 0.35),
        "return_text": False,
        "resolve": "priority",
        "use_llm": bool(options.get("use_llm")),
        "entities": list(options.get("entities") or []),
        "llm_provider": options.get("llm_provider") or "",
        "llm_model": options.get("llm_model") or "",
        "llm_api_key": options.get("llm_api_key") or "",
        "preprocess_obfuscation": bool(options.get("preprocess_obfuscation", True)),
        "preprocess_expanders": list(options.get("preprocess_expanders") or []),
    }
    if options.get("max_chars") is not None:
        kwargs["max_chars"] = options["max_chars"]
    return svc.scan(case["text"], **kwargs)


def evaluate_with_service(
    svc: Any,
    dataset: Mapping[str, Any],
    *,
    options: Optional[Mapping[str, Any]] = None,
    allowed_entity_types: Optional[Sequence[str]] = None,
    progress_cb: Optional[ProgressCb] = None,
    cancel_event: Optional[threading.Event] = None,
    require_redibis_version: bool = False,
) -> dict[str, Any]:
    """Validate, scan, and score one dataset. Persist nothing."""
    opts = dict(options or {})
    allowed = {str(v).strip().upper() for v in (allowed_entity_types or ()) if str(v).strip()}
    if not allowed:
        try:
            allowed = {
                str(row.get("entity_type") or "").upper()
                for row in (svc.entities() or {}).get("entities", [])
                if row.get("entity_type")
            }
        except Exception:
            allowed = set()
    normalized = validate_dataset(
        dataset,
        allowed_entity_types=allowed or None,
        default_language=str(opts.get("language") or "en"),
        require_redibis_version=require_redibis_version,
    )
    predictions: dict[str, Any] = {}
    engines_ran: set[str] = set()
    engines_unavailable: dict[str, str] = {}
    ruleset_id = ""
    ruleset_version = ""
    total = len(normalized["cases"])
    for index, case in enumerate(normalized["cases"]):
        if cancel_event is not None and cancel_event.is_set():
            raise EvalCancelled("evaluation cancelled")
        if progress_cb is not None:
            progress_cb(
                "case",
                {
                    "index": index,
                    "total": total,
                    "id": case["id"],
                    "percent": int(round(100 * index / total)) if total else 100,
                },
            )
        result = _scan_case(svc, case, opts)
        unavailable = dict(getattr(result, "engines_unavailable", {}) or {})
        engines_unavailable.update(
            {str(name): str(reason) for name, reason in unavailable.items()}
        )
        if opts.get("use_llm") and "llm" in unavailable:
            raise DatasetValidationError(
                f"LLM refiner unavailable: {unavailable['llm']}"
            )
        predictions[case["id"]] = result.detections
        engines_ran.update(getattr(result, "engines_ran", ()) or ())
        ruleset_id = getattr(result, "ruleset_id", "") or ruleset_id
        ruleset_version = getattr(result, "ruleset_version", "") or ruleset_version
    if progress_cb is not None:
        progress_cb("score", {"percent": 100})
    profile_id = str(opts.get("normalization") or opts.get("normalization_profile") or PROFILE_V1_ID)
    try:
        profile = load_profile(profile_id)
    except Exception as exc:
        raise DatasetValidationError(str(exc)) from exc
    raw_tiers = opts.get("tiers") or opts.get("tier") or DEFAULT_TIERS
    if isinstance(raw_tiers, str):
        tiers = tuple(part.strip() for part in raw_tiers.split(",") if part.strip())
    else:
        tiers = tuple(str(part).strip() for part in raw_tiers if str(part).strip())
    report = evaluate_dataset(
        normalized,
        predictions,
        overlap_iou=float(opts.get("overlap_iou") or 0.5),
        allowed_entity_types=allowed or None,
        default_language=str(opts.get("language") or "en"),
        require_redibis_version=require_redibis_version,
        profile=profile,
        tiers=tiers,
        normalization_profile=profile_id,
    )
    report["redibis_version"] = current_redibis_version()
    report["scan_config"] = {
        "language": opts.get("language") or "en",
        "engines": opts.get("engines") or "both",
        "min_score": float(opts.get("min_score") or 0.35),
        "use_llm": bool(opts.get("use_llm")),
        "llm_provider": opts.get("llm_provider") or "",
        "llm_model": opts.get("llm_model") or "",
        "preprocess_obfuscation": bool(opts.get("preprocess_obfuscation", True)),
        "overlap_iou": float(opts.get("overlap_iou") or 0.5),
        "normalization": profile_id,
        "tiers": list(tiers),
    }
    overlay = getattr(getattr(svc, "_ruleset", None), "text_rules", None)
    checksum = str(opts.get("rules_checksum") or "")
    extra = {
        "ruleset_id": ruleset_id,
        "ruleset_version": ruleset_version,
        "engines_ran": sorted(engines_ran),
        "engines_unavailable": engines_unavailable,
    }
    extra.update(collect_scan_inventory(svc, opts))
    if not checksum:
        if overlay is None:
            extra["provenance_degraded"] = True
            extra["provenance_degraded_reason"] = "rules overlay unavailable; checksum omitted"
            checksum = ""
        else:
            checksum = rules_checksum(overlay)
    pack_stack = opts.get("pack_stack_header")
    if isinstance(pack_stack, dict) and pack_stack.get("provenance_degraded"):
        extra["provenance_degraded"] = True
        extra["provenance_degraded_reason"] = str(
            pack_stack.get("provenance_degraded_reason") or extra.get("provenance_degraded_reason") or "pack identity failed"
        )
    try:
        prov_obj = getattr(svc, "scan_provenance", lambda *a, **k: None)()
    except Exception:
        prov_obj = None
    if prov_obj is not None:
        extra["provenance_uuid"] = getattr(prov_obj, "provenance_uuid", "") or extra.get("provenance_uuid") or ""
        extra["provenance_degraded"] = bool(
            extra.get("provenance_degraded") or getattr(prov_obj, "provenance_degraded", False)
        )
        extra["provenance_degraded_reason"] = extra.get("provenance_degraded_reason") or getattr(
            prov_obj, "provenance_degraded_reason", ""
        ) or ""
        extra["stack_uuid"] = getattr(prov_obj, "stack_uuid", "") or extra.get("stack_uuid") or ""
        extra["stack_sha256"] = getattr(prov_obj, "stack_sha256", "") or extra.get("stack_sha256") or ""
        if getattr(prov_obj, "regex_inventory", None) and not extra.get("regex_inventory"):
            extra["regex_inventory"] = list(getattr(prov_obj, "regex_inventory", ()) or ())
        if getattr(prov_obj, "ner_backend", None) and not extra.get("ner_backend"):
            extra["ner_backend"] = getattr(prov_obj, "ner_backend", "") or ""
        if getattr(prov_obj, "ner_model_id", None) and not extra.get("ner_model_id"):
            extra["ner_model_id"] = getattr(prov_obj, "ner_model_id", "") or ""
        if getattr(prov_obj, "ner_model_sha256", None) and not extra.get("ner_model_sha256"):
            extra["ner_model_sha256"] = getattr(prov_obj, "ner_model_sha256", "") or ""
    report["provenance"] = base_provenance(
        run_uuid=new_run_uuid(opts.get("run_uuid")),
        label=str(opts.get("label") or ""),
        rules_checksum_value=checksum,
        rules_source=str(opts.get("rules_source") or ""),
        normalization_profile=profile_id,
        pack_stack=pack_stack if isinstance(pack_stack, dict) else None,
        tiers=list(tiers),
        extra=extra,
    )
    try:
        from redibis.pii.run_store import record_run

        record_run(
            kind="eval",
            provenance=prov_obj,
            char_count=sum(len(c.get("text") or "") for c in normalized.get("cases") or []),
            case_count=int(report.get("case_count") or len(normalized.get("cases") or [])),
            outcome={
                "exact_f1": ((report.get("exact") or {}).get("micro") or {}).get("f1"),
                "value_f1": ((report.get("value") or {}).get("micro") or {}).get("f1"),
                "gate": (report.get("gates") or {}).get("summary"),
                "gate_passed": (
                    bool((report.get("gates") or {}).get("passed"))
                    if (report.get("gates") or {}).get("passed") is not None
                    else None
                ),
                "rules_checksum": checksum,
            },
            actor="eval",
            label=str(opts.get("label") or ""),
            run_uuid=report["provenance"].get("run_uuid"),
            parent_run_uuid=str(opts.get("parent_run_uuid") or ""),
        )
    except Exception:
        pass
    return report


__all__ = [
    "EvalCancelled",
    "DatasetValidationError",
    "eval_limiter_weight",
    "evaluate_with_service",
]
