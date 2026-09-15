"""Text Gateway — paste text, see PII, review a de-id policy.

Thin wrapper over TextPIIService. Stateless: nothing is stored, and the
request body is never logged.

Why wrap instead of calling /api/pii/text/* from the browser:
  - the UI size cap is a UI concern, separate from the service 413 gate
  - explorers can scan without opening the admin-only /api/pii/text/* surface
  - one place to force return_text and to emit the analyser envelope
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from typing import Any, Optional

from fastapi import HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from redibis import __version__ as REDIBIS_VERSION
from redibis.services.text_pii_service import (
    TextPIIService,
    TextPIIServiceError,
    text_pii_service_from_env,
)
from redibis.webapp.security import SlidingWindowLimiter

logger = logging.getLogger("redibis.webapp.gateway")

UI_MAX_CHARS = 20_000
SCAN_LIMIT = 30
SCAN_WINDOW_S = 60
# Free-standing guard checks (no PII highlighting) get their own, slightly
# more generous budget since they are meant for pre-flight chat gating.
GUARD_LIMIT = 60
GUARD_WINDOW_S = 60
EVAL_MAX_CASES = 100
EVAL_MAX_TOTAL_CHARS = 200_000

KEY_FIELDS = (
    "master_key",
    "master_key_hex",
    "seed",
    "key",
    "keys",
    "run_key_ref",
    "run_key_ref",
    "api_key",
    "llm_api_key",
)

_get_service = text_pii_service_from_env
SCAN_LIMITER = SlidingWindowLimiter(SCAN_LIMIT, SCAN_WINDOW_S)
GUARD_LIMITER = SlidingWindowLimiter(GUARD_LIMIT, GUARD_WINDOW_S)


class GatewayScanBody(BaseModel):
    text: str
    language: str = "en"
    engines: str = "both"
    min_score: float = Field(default=0.35, ge=0.0, le=1.0)
    resolve: str = "priority"
    use_llm: bool = False
    entities: list[str] = Field(default_factory=list)
    # Per-run provider/model selection for the "Use LLM refiner" path,
    # validated against the shared provider registry (never a raw endpoint).
    llm_provider: str = ""
    llm_model: str = ""
    llm_api_key: str = Field(default="", max_length=2048)
    # Opt-in safety analysers — heuristic checks are free; an LLM judge is
    # additionally used only when the corresponding gateway.* capability
    # role is configured (Settings → LLM → Capability roles).
    check_toxicity: bool = False
    check_prompt_injection: bool = False


class GatewaySuggestBody(GatewayScanBody):
    policy_id: str = "suggested"


class GatewayDeidBody(GatewayScanBody):
    policy_id: Optional[str] = None
    policy: Optional[dict] = None


class GatewayGuardBody(BaseModel):
    text: str
    check_toxicity: bool = True
    check_prompt_injection: bool = True


class GatewayEvaluationBody(BaseModel):
    dataset: dict
    language: str = "en"
    engines: str = "both"
    min_score: float = Field(default=0.35, ge=0.0, le=1.0)
    use_llm: bool = False
    llm_provider: str = ""
    llm_model: str = ""
    llm_api_key: str = Field(default="", max_length=2048)
    overlap_iou: float = Field(default=0.5, gt=0.0, le=1.0)
    draft_rules: Optional[dict] = None
    pack: str = ""
    normalization: str = "v1"
    tier: list[str] | str = "strict,value,overlap,type"
    persist: bool = True
    label: str = ""
    run_uuid: str = ""


class CorpusPatchBody(BaseModel):
    dataset: dict[str, Any]
    actions: list[dict[str, Any]] = Field(default_factory=list)


class UseCaseCreateBody(BaseModel):
    name: str = "untitled"
    text: str = ""
    language: str = "ar"
    tags: list[str] = Field(default_factory=list)
    author: str = ""
    notes: str = ""
    context: dict[str, Any] = Field(default_factory=dict)
    expected_spans: list[dict[str, Any]] = Field(default_factory=list)
    forbidden_spans: list[dict[str, Any]] = Field(default_factory=list)
    rule_edits: dict[str, Any] = Field(default_factory=dict)


class UseCaseSaveBody(BaseModel):
    name: Optional[str] = None
    text: Optional[str] = None
    language: Optional[str] = None
    tags: Optional[list[str]] = None
    author: Optional[str] = None
    notes: Optional[str] = None
    context: Optional[dict[str, Any]] = None
    expected_spans: Optional[list[dict[str, Any]]] = None
    forbidden_spans: Optional[list[dict[str, Any]]] = None
    rule_edits: Optional[dict[str, Any]] = None


class UseCaseRunBody(BaseModel):
    draft_rules: Optional[dict[str, Any]] = None
    context: Optional[dict[str, Any]] = None
    engines: str = "both"
    min_score: float = Field(default=0.35, ge=0.0, le=1.0)
    language: str = ""
    use_llm: bool = False
    persist: bool = True


def _svc() -> TextPIIService:
    return _get_service()


def _actor(request: Request) -> str:
    user = getattr(request.state, "user", None)
    return getattr(user, "username", "") or "anonymous"


def _clip(text: str) -> tuple[str, int, bool]:
    original = text or ""
    original_n = len(original)
    return original[:UI_MAX_CHARS], original_n, original_n > UI_MAX_CHARS


def _guard(request: Request, text: str) -> tuple[str, int, bool]:
    if not (text or "").strip():
        raise HTTPException(status_code=400, detail="text is required")
    if not SCAN_LIMITER.allow(f"gateway:{_actor(request)}"):
        raise HTTPException(
            status_code=429,
            detail="too many scans — wait a moment and try again",
        )
    return _clip(text)


def _no_store(response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _json_no_store(payload: dict, *, status_code: int = 200) -> JSONResponse:
    resp = JSONResponse(payload, status_code=status_code)
    _no_store(resp)
    return resp


def _usecase_store():
    from redibis.pii.usecase_store import UseCaseStore

    return UseCaseStore()


def _usecase_http_error(exc: Exception) -> HTTPException:
    from redibis.pii.usecase_store import UseCaseValidationError

    if isinstance(exc, UseCaseValidationError):
        return HTTPException(
            status_code=422,
            detail={"message": str(exc), "span_id": exc.span_id, "value": exc.value},
        )
    return HTTPException(status_code=400, detail=str(exc))


def _dataset_from_usecase(uc, *, language: str = "") -> dict:
    from redibis.pii.eval.span_metrics import DATASET_KIND, SCHEMA_VERSION_1_2

    return {
        "kind": DATASET_KIND,
        "schema_version": SCHEMA_VERSION_1_2,
        "id": uc.id,
        "cases": [{
            "id": uc.id,
            "text": uc.text,
            "language": language or uc.language or "ar",
            "tags": list(uc.tags),
            "expected_spans": [dict(s) for s in uc.expected_spans],
            "forbidden_spans": [dict(s) for s in uc.forbidden_spans],
        }],
    }


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except TextPIIServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


def _pick(d: dict, *names, default=None):
    for name in names:
        if name in d and d[name] is not None:
            return d[name]
    return default


def _redibis_config() -> Any:
    """Best-effort access to the config the active service was built from."""
    try:
        cfg = getattr(_svc(), "config", None)
        if cfg is not None:
            return cfg
    except Exception:
        pass
    from redibis.config import RedibisConfig

    return RedibisConfig.default()


def _guard_thresholds(cfg: Any) -> Any:
    return getattr(getattr(cfg, "text_gateway", None), "thresholds", None)


def envelope(
    result,
    *,
    truncated: bool,
    original_char_count: int,
    guards: Optional[dict] = None,
    redibis_config: Any = None,
) -> dict:
    """G1 shape of the G5/G6 analyser envelope. Client contract — keep stable
    for existing keys; ``prompt_injection`` and populated ``toxicity`` /
    ``decision`` are the G6 additions (opt-in via request flags)."""
    d = result.to_dict(return_text=True)
    spans = list(_pick(d, "spans", "detections", default=[]) or [])
    guards = guards or {}

    analysers: dict[str, Any] = {
        "pii": {
            "status": "ok",
            "spans": spans,
            "entity_counts": dict(
                _pick(d, "entity_counts", "entity_counts", default={}) or {}
            ),
            "engines_ran": list(
                _pick(d, "engines_ran", "engines_ran", default=[]) or []
            ),
            "engines_unavailable": dict(_pick(d, "engines_unavailable", default={}) or {}),
            "ruleset_id": _pick(d, "ruleset_id", "ruleset_id", default=""),
            "ruleset_version": _pick(
                d, "ruleset_version", "ruleset_version", default=""
            ),
        },
        "toxicity": (
            guards["toxicity"].to_dict() if "toxicity" in guards else {"status": "not_configured"}
        ),
        "prompt_injection": (
            guards["prompt_injection"].to_dict()
            if "prompt_injection" in guards
            else {"status": "not_configured"}
        ),
        "sentiment": {"status": "not_configured"},
    }

    decision = {"action": "allow", "reasons": []}
    if guards:
        from redibis.pii.text_guards import strongest_action

        action, reasons = strongest_action(
            guards, thresholds=_guard_thresholds(redibis_config)
        )
        decision = {"action": action, "reasons": reasons}

    out = {
        "text_meta": {
            "char_count": int(_pick(d, "char_count", default=0) or 0),
            "original_char_count": original_char_count,
            "truncated": bool(truncated or _pick(d, "truncated", default=False)),
            "language": _pick(d, "language", default="en"),
            "offset_unit": _pick(
                d, "offset_unit", "offset_unit", default="unicode_codepoint"
            ),
        },
        "analysers": analysers,
        "decision": decision,
        "provenance_uuid": _pick(d, "provenance_uuid", default="") or "",
        "run_uuid": _pick(d, "run_uuid", default="") or "",
        "provenance_degraded": bool(_pick(d, "provenance_degraded", default=False)),
    }
    if d.get("provenance"):
        out["provenance"] = d["provenance"]
    return out


def _llm_trace(
    *,
    run_id: str,
    requested: bool,
    result: Any,
    include_transcripts: bool,
) -> dict:
    """Admin-facing LLM usage + transcripts for this Gateway run.

    Explorers still learn whether a model ran (``used`` / ``call_count``) but
    never receive prompt or response bodies.
    """
    from redibis.enrich.llm_logging import get_recent_llm_calls

    d = result.to_dict(return_text=True) if result is not None and hasattr(result, "to_dict") else {}
    engines = list(_pick(d, "engines_ran", default=[]) or [])
    unavailable = dict(_pick(d, "engines_unavailable", default={}) or {})
    rid = (run_id or "").strip()
    calls = get_recent_llm_calls(
        run_id=rid,
        limit=200,
        include_transcripts=include_transcripts,
    ) if rid else []
    pii_ran = "llm" in engines
    skip = str(unavailable.get("llm") or "")
    used = pii_ran or any((c.get("status") or "") == "ok" for c in calls)
    if not requested and not calls and not pii_ran:
        skip = skip or "not requested"
    return {
        "requested": bool(requested),
        "used": bool(used),
        "pii_refiner_ran": pii_ran,
        "skip_reason": "" if used else skip,
        "call_count": len(calls),
        "run_id": rid,
        "providers": sorted({str(c.get("provider") or "") for c in calls if c.get("provider")}),
        "models": sorted({str(c.get("model_id") or "") for c in calls if c.get("model_id")}),
        "calls": calls,
    }


@contextmanager
def _gateway_llm_run():
    from redibis.enrich.llm_logging import llm_call_context
    from redibis.pii.eval.provenance import new_run_uuid

    run_id = new_run_uuid()
    with llm_call_context(run_id=run_id, model_role="gateway"):
        yield run_id


def _attach_llm_trace(
    payload: dict,
    *,
    request: Request,
    requested: bool,
    result: Any,
    run_id: str,
) -> dict:
    from redibis.webapp.pii_text_routes import role_may_see_full_provenance

    include = role_may_see_full_provenance(request)
    if not payload.get("run_uuid"):
        payload["run_uuid"] = run_id
    payload["llm"] = _llm_trace(
        run_id=run_id or str(payload.get("run_uuid") or ""),
        requested=requested,
        result=result,
        include_transcripts=include,
    )
    return payload


def _guard_role_health(role: str, *, llm_enabled: bool) -> dict:
    """Cheap (no model call) check of whether a guard's LLM step will
    actually run.

    A role can resolve to a provider purely by inheriting the operator's
    root default LLM (``llm.default``) even though nobody opted this
    *specific* gateway guard into LLM use — ``_run_guards`` still gates the
    call on ``text_gateway.{toxicity,prompt_injection}_llm_enabled``. Report
    ``configured: True`` only when both are true, so the health payload
    (and the "toxicity LLM: configured" UI hint) never promises a call that
    ``_run_guards`` will not actually make.
    """
    if not llm_enabled:
        return {
            "configured": False,
            "reason": f"gateway LLM guard disabled for {role!r} (text_gateway config)",
        }
    from redibis.config import load_global_settings_optional
    from redibis.enrich.capability_routing import RoutingError, get_provider_for_role

    try:
        gs = load_global_settings_optional()
    except Exception:
        gs = None
    try:
        _provider, binding = get_provider_for_role(role, gs=gs)
        return {"configured": True, "provider": binding.provider, "model": binding.model}
    except RoutingError as exc:
        return {"configured": False, "reason": str(exc)}
    except Exception as exc:  # pragma: no cover - defensive
        return {"configured": False, "reason": str(exc)}


def _safe_llm_providers() -> list[dict]:
    """Provider names/metadata only — never keys/endpoints — for the
    Text Gateway's per-run provider selector."""
    try:
        from redibis.enrich.providers import list_providers

        return [
            {
                "name": p.get("name"),
                "description": p.get("description"),
                "cloud": bool(p.get("cloud")),
                "offline": bool(p.get("offline")),
                "needs_key": bool(p.get("needs_key")),
                "api_key_env_set": bool(p.get("api_key_env_set")),
                "api_key_saved": bool(p.get("api_key_saved")),
                "kind": p.get("kind"),
                "residency": p.get("residency"),
                "api_base": p.get("api_base"),
                "default_model_bare": p.get("default_model_bare"),
            }
            for p in list_providers()
        ]
    except Exception:
        return []


def _text_refiner_health(cfg: Any, providers: list[dict]) -> dict:
    """Resolve the effective Gateway role and explain config-level readiness."""
    from redibis.config import load_global_settings_optional
    from redibis.enrich.capability_routing import RoutingError, get_provider_for_role

    try:
        gs = load_global_settings_optional()
    except Exception:
        gs = None
    try:
        provider, binding = get_provider_for_role(
            "pii.text_refiner",
            gs=gs,
            agents_cfg=getattr(cfg, "agents", None),
        )
    except RoutingError as exc:
        return {
            "role": "pii.text_refiner",
            "role_bound": False,
            "ready": False,
            "status": "unbound",
            "reason": str(exc),
        }
    except Exception as exc:
        return {
            "role": "pii.text_refiner",
            "role_bound": False,
            "ready": False,
            "status": "configuration_error",
            "reason": str(exc),
        }

    from redibis.enrich.providers import provider_is_credential_ready

    metadata = next(
        (row for row in providers if row.get("name") == binding.provider), {}
    )
    model = binding.model or metadata.get("default_model_bare") or ""
    cloud = bool(metadata.get("cloud"))
    key_ready = provider_is_credential_ready(metadata) if metadata else True
    pii_llm = getattr(getattr(cfg, "pii", None), "llm", None)
    external_allowed = bool(getattr(pii_llm, "allow_external_raw_text", False))
    if not key_ready:
        status = "missing_credentials"
        reason = (
            f"{binding.provider} requires its configured API-key environment variable; "
            "set it and restart the webapp"
        )
    elif cloud and not external_allowed:
        status = "raw_text_external_blocked"
        reason = (
            "cloud free-text inference is disabled; enable "
            "pii.llm.allow_external_raw_text only when policy permits"
        )
    else:
        status = "configured"
        reason = "configuration is ready; use the role test to verify connectivity"
    return {
        "role": "pii.text_refiner",
        "role_bound": True,
        "provider": binding.provider or getattr(provider, "name", ""),
        "model": model,
        "cloud": cloud,
        "credential_ready": key_ready,
        "ready": key_ready and (not cloud or external_allowed),
        "status": status,
        "reason": reason,
        "source": binding.source,
    }


def _run_guards(body: GatewayScanBody, text: str, redibis_config: Any) -> dict:
    """Run the requested opt-in safety analysers. Never raises — a failed
    analyser degrades to its own ``error``/``heuristic_only`` status, it
    never silently disappears from the envelope."""
    from redibis.pii.text_guards import check_prompt_injection, check_toxicity

    gw_cfg = getattr(redibis_config, "text_gateway", None)
    out: dict[str, Any] = {}
    if body.check_toxicity:
        out["toxicity"] = check_toxicity(
            text,
            redibis_config=redibis_config,
            use_llm=bool(getattr(gw_cfg, "toxicity_llm_enabled", False)),
            heuristic_enabled=bool(getattr(gw_cfg, "heuristic_toxicity", True)),
        )
    if body.check_prompt_injection:
        out["prompt_injection"] = check_prompt_injection(
            text,
            redibis_config=redibis_config,
            use_llm=bool(getattr(gw_cfg, "prompt_injection_llm_enabled", False)),
            heuristic_enabled=bool(getattr(gw_cfg, "heuristic_prompt_injection", True)),
        )
    return out


def _scan_kwargs(body: GatewayScanBody, max_chars: int) -> dict:
    gw_cfg = getattr(_redibis_config(), "text_gateway", None)
    preprocess = True
    expanders: list[str] = []
    if gw_cfg is not None:
        preprocess = bool(getattr(gw_cfg, "obfuscation_preprocess", True))
        expanders = list(getattr(gw_cfg, "obfuscation_expanders", None) or [])
    return {
        "language": body.language,
        "engines": body.engines,
        "min_score": body.min_score,
        "return_text": True,
        "resolve": body.resolve,
        "use_llm": body.use_llm,
        "entities": body.entities,
        "max_chars": max_chars,
        "llm_provider": body.llm_provider,
        "llm_model": body.llm_model,
        "llm_api_key": (body.llm_api_key or "").strip(),
        "preprocess_obfuscation": preprocess,
        "preprocess_expanders": expanders,
    }


def _strip_keys(payload: dict) -> dict:
    for forbidden in KEY_FIELDS:
        payload.pop(forbidden, None)
    nested = payload.get("deidentified")
    if isinstance(nested, dict):
        for forbidden in KEY_FIELDS:
            nested.pop(forbidden, None)
        nested.pop("original_spans", None)
        nested.pop("original_spans", None)
    return payload


def _deidentified_block(deid: Any) -> dict:
    data = deid.to_dict() if hasattr(deid, "to_dict") else dict(deid)
    return {
        "text": data.get("deidentified_text")
        or data.get("deidentified_text")
        or "",
        "applied": list(data.get("applied") or []),
        "policy_id": data.get("policy_id") or data.get("policy_id") or "",
        "policy_version": data.get("policy_version")
        or data.get("policy_version")
        or "",
        "reversible_spans": int(
            data.get("reversible_spans") or data.get("reversible_spans") or 0
        ),
    }


def _prepare_evaluation(body: GatewayEvaluationBody, actor: str) -> tuple[dict, dict, int]:
    from redibis.pii.eval import DatasetValidationError, eval_limiter_weight, validate_dataset

    catalogue = _call(_svc().entities).get("entities", [])
    allowed = {
        str(row.get("entity_type") or "").upper()
        for row in catalogue
        if row.get("entity_type")
    }
    if not allowed:
        raise HTTPException(
            status_code=503,
            detail="PII entity catalogue is unavailable; evaluation cannot validate labels",
        )
    try:
        dataset = validate_dataset(body.dataset, allowed_entity_types=allowed)
    except DatasetValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if len(dataset["cases"]) > EVAL_MAX_CASES:
        raise HTTPException(
            status_code=413,
            detail=f"evaluation exceeds max cases ({EVAL_MAX_CASES})",
        )
    oversized = [
        case["id"] for case in dataset["cases"] if len(case["text"]) > UI_MAX_CHARS
    ]
    if oversized:
        raise HTTPException(
            status_code=413,
            detail=f"{len(oversized)} case(s) exceed {UI_MAX_CHARS} characters",
        )
    total_chars = sum(len(case["text"]) for case in dataset["cases"])
    if total_chars > EVAL_MAX_TOTAL_CHARS:
        raise HTTPException(
            status_code=413,
            detail=f"evaluation exceeds total character limit ({EVAL_MAX_TOTAL_CHARS})",
        )
    weight = eval_limiter_weight(
        len(dataset["cases"]), total_chars, use_llm=body.use_llm
    )
    if not SCAN_LIMITER.allow(f"gateway-eval:{actor}", weight=weight):
        raise HTTPException(
            status_code=429,
            detail="too many evaluations — wait a moment and try again",
        )
    cfg = _redibis_config()
    if body.use_llm:
        providers = _safe_llm_providers()
        if body.llm_provider:
            from redibis.enrich.providers import provider_is_credential_ready

            metadata = next(
                (row for row in providers if row.get("name") == body.llm_provider),
                None,
            )
            if metadata is not None and not provider_is_credential_ready(metadata):
                if not (body.llm_api_key or "").strip():
                    raise HTTPException(
                        status_code=409,
                        detail=f"LLM provider {body.llm_provider!r} is missing credentials",
                    )
        else:
            readiness = _text_refiner_health(cfg, providers)
            if not readiness.get("ready"):
                raise HTTPException(
                    status_code=409,
                    detail=str(readiness.get("reason") or "LLM refiner is not ready"),
                )
    gw_cfg = getattr(cfg, "text_gateway", None)
    options = {
        "language": body.language,
        "engines": body.engines,
        "min_score": body.min_score,
        "use_llm": body.use_llm,
        "llm_provider": body.llm_provider,
        "llm_model": body.llm_model,
        "llm_api_key": (body.llm_api_key or "").strip(),
        "max_chars": UI_MAX_CHARS,
        "overlap_iou": body.overlap_iou,
        "preprocess_obfuscation": bool(
            getattr(gw_cfg, "obfuscation_preprocess", True)
        ),
        "preprocess_expanders": list(
            getattr(gw_cfg, "obfuscation_expanders", None) or []
        ),
        "normalization": body.normalization or "v1",
        "tiers": body.tier,
        "label": body.label or "",
        "run_uuid": body.run_uuid or "",
        "draft_rules": body.draft_rules,
        "pack": body.pack or "",
        "persist": bool(body.persist),
    }
    return dataset, options, total_chars


def _run_evaluation(dataset: dict, options: dict, *, progress_cb=None, cancel_event=None) -> dict:
    from redibis.pii.eval import DatasetValidationError, evaluate_with_service
    from redibis.pii.eval.registry import put_run
    from redibis.pii.text_rules import TextRuleOverlay, merge_text_rules
    from redibis.services.text_pii_service import TextPIIServiceError

    svc = _svc()
    draft = options.get("draft_rules")
    pack = options.get("pack") or ""
    if draft or pack:
        overlay = None
        pack_stack = None
        pack_header = None
        if draft:
            overlay = merge_text_rules(
                getattr(getattr(svc, "_ruleset", None), "text_rules", None),
                TextRuleOverlay.from_dict(draft),
            )
            options["rules_source"] = "service+draft"
        if pack:
            from redibis.pii.eval.pinning import EvalPinError, resolve_pack_stack

            try:
                pack_stack, pack_header = resolve_pack_stack(
                    pack=str(pack), redibis_config=_redibis_config()
                )
            except EvalPinError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            options["pack_stack_header"] = pack_header
        svc = TextPIIService(
            redibis_config=_redibis_config(),
            text_rules_overlay=overlay,
            skip_stored_text_rules=overlay is not None,
            merge_builtin_text_rules=overlay is None,
            pack_stack=pack_stack,
            ner_backend=getattr(_svc(), "_ner", None),
        )
    try:
        report = evaluate_with_service(
            svc,
            dataset,
            options=options,
            progress_cb=progress_cb,
            cancel_event=cancel_event,
        )
    except DatasetValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TextPIIServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    if options.get("persist", True):
        put_run(report)
    return report


def register_gateway_routes(
    app: Any,
    templates: Any,
    asset_v_fn,
    page_context_fn,
    service_factory=None,
    limiter=None,
) -> None:
    global _get_service, SCAN_LIMITER
    if service_factory is not None:
        _get_service = service_factory
    if limiter is not None:
        SCAN_LIMITER = limiter

    @app.get("/gateway", response_class=HTMLResponse)
    async def gateway_page(request: Request):
        resp = templates.TemplateResponse(
            request=request,
            name="gateway.html",
            context=page_context_fn(
                request,
                js_v=asset_v_fn("gateway.js"),
                css_v=asset_v_fn("gateway.css"),
                ui_max_chars=UI_MAX_CHARS,
            ),
        )
        _no_store(resp)
        return resp

    @app.get("/gateway/evaluations", response_class=HTMLResponse)
    async def gateway_evaluations_page(request: Request):
        resp = templates.TemplateResponse(
            request=request,
            name="gateway_eval.html",
            context=page_context_fn(
                request,
                js_v=asset_v_fn("gateway_eval.js"),
                css_v=asset_v_fn("gateway.css"),
                ui_max_chars=UI_MAX_CHARS,
                eval_max_cases=EVAL_MAX_CASES,
                redibis_version=REDIBIS_VERSION,
            ),
        )
        _no_store(resp)
        return resp

    @app.get("/gateway/usecases", response_class=HTMLResponse)
    async def gateway_usecase_list_page(request: Request):
        resp = templates.TemplateResponse(
            request=request,
            name="gateway_usecase.html",
            context=page_context_fn(
                request,
                js_v=asset_v_fn("gateway_usecase.js"),
                css_v=asset_v_fn("gateway.css"),
                ui_max_chars=UI_MAX_CHARS,
                uc_id="",
            ),
        )
        _no_store(resp)
        return resp

    @app.get("/gateway/usecases/{uc_id}", response_class=HTMLResponse)
    async def gateway_usecase_page(request: Request, uc_id: str):
        resp = templates.TemplateResponse(
            request=request,
            name="gateway_usecase.html",
            context=page_context_fn(
                request,
                js_v=asset_v_fn("gateway_usecase.js"),
                css_v=asset_v_fn("gateway.css"),
                ui_max_chars=UI_MAX_CHARS,
                uc_id=uc_id,
            ),
        )
        _no_store(resp)
        return resp

    @app.get("/gateway/runs/{run_uuid}", response_class=HTMLResponse)
    async def gateway_run_page(request: Request, run_uuid: str):
        from redibis.pii.eval.registry import get_run
        from redibis.pii.eval.report_html import render_report_body, report_css

        report = get_run(run_uuid)
        report_body = ""
        usecase_id = ""
        if report is not None:
            try:
                report_body = render_report_body(report)
            except Exception as exc:
                logger.warning("gateway run report render failed: %s", exc)
            usecase_id = str((report.get("usecase") or {}).get("id") or "")
        resp = templates.TemplateResponse(
            request=request,
            name="gateway_run.html",
            context=page_context_fn(
                request,
                js_v=asset_v_fn("gateway_run.js"),
                css_v=asset_v_fn("gateway.css"),
                ui_max_chars=UI_MAX_CHARS,
                run_uuid=run_uuid,
                usecase_id=usecase_id,
                report_css=report_css(),
                report_body=report_body,
                report_missing=report is None,
            ),
        )
        _no_store(resp)
        return resp

    @app.get("/api/gateway/health")
    async def gateway_health(request: Request) -> Any:
        """Truthful engine/provider availability — safe fields only (no keys)."""
        health = _call(_svc().health)
        cfg = _redibis_config()
        gw_cfg = getattr(cfg, "text_gateway", None)
        toxicity_llm_enabled = bool(getattr(gw_cfg, "toxicity_llm_enabled", False))
        prompt_injection_llm_enabled = bool(getattr(gw_cfg, "prompt_injection_llm_enabled", False))
        providers = _safe_llm_providers()
        default_llm = _text_refiner_health(cfg, providers)
        try:
            catalogue = list((_svc().entities() or {}).get("entities") or [])
        except Exception:
            catalogue = []
        payload = {
            "engines": health.get("engines", {}),
            "guards": {
                "toxicity": _guard_role_health(
                    "gateway.toxicity", llm_enabled=toxicity_llm_enabled
                ),
                "prompt_injection": _guard_role_health(
                    "gateway.prompt_injection", llm_enabled=prompt_injection_llm_enabled
                ),
            },
            "llm_providers": providers,
            "entity_catalogue": catalogue,
            "text_gateway": {
                "toxicity_llm_enabled": toxicity_llm_enabled,
                "prompt_injection_llm_enabled": prompt_injection_llm_enabled,
                "obfuscation_preprocess": bool(
                    getattr(gw_cfg, "obfuscation_preprocess", True)
                ),
                "obfuscation_expanders": list(
                    getattr(gw_cfg, "obfuscation_expanders", None) or []
                ),
            },
            "default_llm": default_llm,
        }
        return _json_no_store(payload)

    @app.post("/api/gateway/evaluations/run")
    async def gateway_evaluations_run(
        request: Request, body: GatewayEvaluationBody
    ) -> Any:
        """Scan and score a portable dataset in memory; persist nothing."""
        from starlette.concurrency import run_in_threadpool

        dataset, options, total_chars = _prepare_evaluation(body, _actor(request))
        report = await run_in_threadpool(_run_evaluation, dataset, options)
        logger.info(
            "gateway_evaluation actor=%s cases=%s chars=%s exact_f1=%s overlap_f1=%s",
            _actor(request),
            len(dataset["cases"]),
            total_chars,
            report["exact"]["micro"]["f1"],
            report["overlap"]["micro"]["f1"],
        )
        return _json_no_store(report)

    @app.post("/api/gateway/evaluations/run/stream")
    async def gateway_evaluations_run_stream(
        request: Request, body: GatewayEvaluationBody
    ) -> Any:
        """NDJSON progress events, then the evaluation report.

        Disconnect cancels later cases; a currently running model call is
        bounded by the provider timeout.
        """
        import threading

        from starlette.concurrency import run_in_threadpool

        from redibis.pii.eval import EvalCancelled

        dataset, options, total_chars = _prepare_evaluation(body, _actor(request))
        t0 = time.perf_counter()

        async def _events():
            import asyncio

            queue: asyncio.Queue = asyncio.Queue()
            loop = asyncio.get_running_loop()
            done = object()
            cancel_event = threading.Event()

            def _on_progress(stage: str, detail: dict) -> None:
                loop.call_soon_threadsafe(
                    queue.put_nowait, {"event": "stage", "stage": stage, **detail}
                )

            def _do_eval():
                return _run_evaluation(
                    dataset,
                    options,
                    progress_cb=_on_progress,
                    cancel_event=cancel_event,
                )

            async def _run():
                try:
                    report = await run_in_threadpool(_do_eval)
                    await queue.put({"event": "result", "report": report})
                except EvalCancelled:
                    await queue.put({"event": "error", "detail": "evaluation cancelled"})
                except HTTPException as exc:
                    await queue.put({"event": "error", "detail": str(exc.detail)})
                except Exception:
                    logger.exception("gateway_evaluations_run_stream failed")
                    await queue.put({"event": "error", "detail": "evaluation failed"})
                finally:
                    await queue.put(done)

            task = asyncio.ensure_future(_run())
            try:
                while True:
                    if await request.is_disconnected():
                        cancel_event.set()
                        task.cancel()
                        break
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=0.25)
                    except asyncio.TimeoutError:
                        continue
                    if item is done:
                        break
                    yield (json.dumps(item) + "\n").encode("utf-8")
            finally:
                cancel_event.set()
                if not task.done():
                    task.cancel()
            logger.info(
                "gateway_evaluation_stream actor=%s cases=%s chars=%s latency_ms=%.1f",
                _actor(request),
                len(dataset["cases"]),
                total_chars,
                (time.perf_counter() - t0) * 1000,
            )

        resp = StreamingResponse(_events(), media_type="application/x-ndjson")
        _no_store(resp)
        return resp

    @app.get("/api/gateway/evaluations/runs")
    async def gateway_evaluations_runs() -> Any:
        from redibis.pii.eval.registry import list_runs

        return _json_no_store({"runs": list_runs()})

    @app.get("/api/gateway/evaluations/runs/{run_uuid}")
    async def gateway_evaluations_run_get(run_uuid: str) -> Any:
        from redibis.pii.eval.registry import get_run

        report = get_run(run_uuid)
        if report is None:
            raise HTTPException(status_code=404, detail="unknown evaluation run")
        return _json_no_store(report)

    @app.get("/api/gateway/evaluations/runs/{run_uuid}/cases/{case_id}")
    async def gateway_evaluations_case(run_uuid: str, case_id: str) -> Any:
        from redibis.pii.eval.registry import get_case

        row = get_case(run_uuid, case_id)
        if row is None:
            raise HTTPException(status_code=404, detail="unknown evaluation case")
        return _json_no_store(row)

    @app.get("/api/gateway/evaluations/compare")
    async def gateway_evaluations_compare(a: str, b: str) -> Any:
        from redibis.pii.eval.registry import compare_runs

        try:
            return _json_no_store(compare_runs(a, b))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/gateway/evaluations/corpus-patch")
    async def gateway_evaluations_corpus_patch(body: CorpusPatchBody) -> Any:
        from redibis.pii.eval.corpus_patch import apply_corpus_patch, draft_rules_from_actions

        result = apply_corpus_patch(body.dataset, body.actions)
        result["draft_rules"] = draft_rules_from_actions(body.actions)
        return _json_no_store(result)

    @app.get("/api/gateway/usecases")
    async def gateway_usecases_list(tag: str = "", limit: int = 100) -> Any:
        store = _usecase_store()
        return _json_no_store({"usecases": store.list(tag=tag, limit=limit)})

    @app.post("/api/gateway/usecases")
    async def gateway_usecases_create(request: Request, body: UseCaseCreateBody) -> Any:
        from redibis.pii.usecase_store import usecase_from_payload

        store = _usecase_store()
        try:
            draft, relocated = usecase_from_payload(
                body.model_dump(),
                author=_actor(request),
            )
            saved = store.save(draft)
        except Exception as exc:
            raise _usecase_http_error(exc) from exc
        payload = saved.to_dict()
        if relocated:
            payload["relocated"] = relocated
        return _json_no_store(payload, status_code=201)

    @app.post("/api/gateway/usecases/import")
    async def gateway_usecases_import(request: Request) -> Any:
        from redibis.pii.usecase_store import usecase_from_payload

        ctype = (request.headers.get("content-type") or "").lower()
        try:
            if "multipart/form-data" in ctype:
                form = await request.form()
                upload = form.get("file")
                if upload is None:
                    raise HTTPException(status_code=400, detail="file is required")
                raw = await upload.read()
                payload = json.loads(raw.decode("utf-8"))
            else:
                payload = await request.json()
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"invalid use-case asset: {exc}") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="use-case asset must be a JSON object")
        store = _usecase_store()
        existing = None
        uc_id = str(payload.get("id") or "").strip()
        if uc_id:
            try:
                existing = store.get(uc_id)
            except Exception:
                existing = None
        try:
            draft, relocated = usecase_from_payload(
                payload, existing=existing, author=_actor(request)
            )
            saved = store.save(draft)
        except Exception as exc:
            raise _usecase_http_error(exc) from exc
        out = saved.to_dict()
        if relocated:
            out["relocated"] = relocated
        return _json_no_store(out, status_code=201 if existing is None else 200)

    @app.get("/api/gateway/usecases/{uc_id}")
    async def gateway_usecases_get(uc_id: str, version: Optional[int] = None) -> Any:
        try:
            uc = _usecase_store().get(uc_id, version=version)
        except Exception as exc:
            raise _usecase_http_error(exc) from exc
        if uc is None:
            raise HTTPException(status_code=404, detail="unknown use case")
        return _json_no_store(uc.to_dict())

    @app.put("/api/gateway/usecases/{uc_id}")
    async def gateway_usecases_save(
        request: Request, uc_id: str, body: UseCaseSaveBody
    ) -> Any:
        from redibis.pii.usecase_store import usecase_from_payload

        store = _usecase_store()
        try:
            existing = store.get(uc_id)
        except Exception as exc:
            raise _usecase_http_error(exc) from exc
        if existing is None:
            raise HTTPException(status_code=404, detail="unknown use case")
        incoming = existing.to_dict()
        for key, value in body.model_dump(exclude_unset=True).items():
            incoming[key] = value
        incoming["id"] = uc_id
        try:
            draft, relocated = usecase_from_payload(
                incoming, existing=existing, author=_actor(request)
            )
            saved = store.save(draft)
        except Exception as exc:
            raise _usecase_http_error(exc) from exc
        payload = saved.to_dict()
        if relocated:
            payload["relocated"] = relocated
        return _json_no_store(payload)

    @app.delete("/api/gateway/usecases/{uc_id}")
    async def gateway_usecases_delete(uc_id: str) -> Any:
        try:
            ok = _usecase_store().delete(uc_id)
        except Exception as exc:
            raise _usecase_http_error(exc) from exc
        if not ok:
            raise HTTPException(status_code=404, detail="unknown use case")
        return _json_no_store({"deleted": True, "id": uc_id})

    @app.get("/api/gateway/usecases/{uc_id}/download")
    async def gateway_usecases_download(uc_id: str, version: Optional[int] = None) -> Any:
        try:
            uc = _usecase_store().get(uc_id, version=version)
        except Exception as exc:
            raise _usecase_http_error(exc) from exc
        if uc is None:
            raise HTTPException(status_code=404, detail="unknown use case")
        body = json.dumps(uc.to_dict(), indent=2, ensure_ascii=False) + "\n"
        resp = Response(content=body.encode("utf-8"), media_type="application/json")
        resp.headers["Content-Disposition"] = (
            f'attachment; filename="{uc.id}-v{uc.version}.json"'
        )
        _no_store(resp)
        return resp

    @app.post("/api/gateway/usecases/{uc_id}/run")
    async def gateway_usecases_run(
        request: Request, uc_id: str, body: UseCaseRunBody
    ) -> Any:
        from starlette.concurrency import run_in_threadpool

        from redibis.pii.eval.registry import put_run

        try:
            uc = _usecase_store().get(uc_id)
        except Exception as exc:
            raise _usecase_http_error(exc) from exc
        if uc is None:
            raise HTTPException(status_code=404, detail="unknown use case")
        language = body.language or uc.language or "ar"
        dataset = _dataset_from_usecase(uc, language=language)
        total_chars = len(uc.text or "")
        if total_chars > UI_MAX_CHARS:
            raise HTTPException(
                status_code=413,
                detail=f"use case exceeds {UI_MAX_CHARS} characters",
            )
        draft = body.draft_rules if body.draft_rules is not None else dict(uc.rule_edits or {})
        options = {
            "language": language,
            "engines": body.engines,
            "min_score": body.min_score,
            "use_llm": body.use_llm,
            "llm_provider": "",
            "llm_model": "",
            "llm_api_key": "",
            "max_chars": UI_MAX_CHARS,
            "overlap_iou": 0.5,
            "preprocess_obfuscation": True,
            "preprocess_expanders": [],
            "normalization": "v1",
            "tiers": "strict,value,overlap,type",
            "label": f"usecase:{uc.id}@v{uc.version}",
            "run_uuid": "",
            "draft_rules": draft or None,
            "pack": "",
            "persist": bool(body.persist),
        }
        report = await run_in_threadpool(_run_evaluation, dataset, options)
        report["usecase"] = {
            "id": uc.id,
            "version": uc.version,
            "name": uc.name,
            "context": body.context if body.context is not None else dict(uc.context or {}),
        }
        run_uuid = put_run(report)
        return _json_no_store({
            "run_uuid": run_uuid,
            "report_url": f"/gateway/runs/{run_uuid}",
        })

    @app.post("/api/gateway/scan")
    async def gateway_scan(
        request: Request,
        body: GatewayScanBody,
        provenance: str = Query(""),
    ) -> Any:
        text, original_n, truncated = _guard(request, body.text)
        t0 = time.perf_counter()
        cfg = _redibis_config()
        kwargs = _scan_kwargs(body, UI_MAX_CHARS)
        from redibis.webapp.pii_text_routes import role_may_see_full_provenance

        kwargs["include_provenance"] = (
            str(provenance).lower() == "full" and role_may_see_full_provenance(request)
        )
        with _gateway_llm_run() as run_id:
            result = _call(_svc().scan, text, **kwargs)
            guards = _run_guards(body, text, cfg)
        payload = envelope(
            result,
            truncated=truncated,
            original_char_count=original_n,
            guards=guards,
            redibis_config=cfg,
        )
        _attach_llm_trace(
            payload,
            request=request,
            requested=bool(body.use_llm),
            result=result,
            run_id=run_id,
        )
        try:
            from redibis.webapp.pii_text_routes import _record_result

            _record_result(result, text=text, kind="gateway_scan", actor=_actor(request))
        except Exception:
            logger.debug("gateway run registry skipped", exc_info=True)
        pii = payload["analysers"]["pii"]
        logger.info(
            "gateway_scan actor=%s chars=%s original=%s truncated=%s "
            "entities=%s engines=%s engines_unavailable=%s decision=%s latency_ms=%.1f",
            _actor(request),
            payload["text_meta"]["char_count"],
            original_n,
            truncated,
            pii.get("entity_counts"),
            pii.get("engines_ran"),
            pii.get("engines_unavailable"),
            payload["decision"]["action"],
            (time.perf_counter() - t0) * 1000,
        )
        return _json_no_store(payload)

    @app.post("/api/gateway/scan/stream")
    async def gateway_scan_stream(request: Request, body: GatewayScanBody) -> Any:
        """NDJSON progress events, then the same final envelope as ``/scan``.

        Each line is one JSON object: ``{"event": "stage", "stage": "...", ...}``
        for progress, then a final ``{"event": "result", "envelope": {...}}``,
        or ``{"event": "error", "detail": "..."}``. Blocking inference runs off
        the event loop; nothing is persisted and the response is ``no-store``.
        """
        from starlette.concurrency import run_in_threadpool

        text, original_n, truncated = _guard(request, body.text)
        cfg = _redibis_config()
        t0 = time.perf_counter()

        async def _events():
            import asyncio

            queue: asyncio.Queue = asyncio.Queue()
            loop = asyncio.get_running_loop()
            done = object()

            def _on_stage(stage: str, detail: dict) -> None:
                loop.call_soon_threadsafe(
                    queue.put_nowait, {"event": "stage", "stage": stage, **detail}
                )

            def _do_scan():
                with _gateway_llm_run() as run_id:
                    result = _svc().scan(
                        text, progress_cb=_on_stage, **_scan_kwargs(body, UI_MAX_CHARS)
                    )
                    # Guards may call an external LLM (network I/O) when the
                    # matching text_gateway.*_llm_enabled flag is set — keep
                    # that off the event loop too, or the "streamed so it
                    # never blocks" endpoint blocks on exactly that call.
                    guards = _run_guards(body, text, cfg)
                return result, guards, run_id

            async def _run():
                try:
                    result, guards, run_id = await run_in_threadpool(_do_scan)
                    payload = envelope(
                        result,
                        truncated=truncated,
                        original_char_count=original_n,
                        guards=guards,
                        redibis_config=cfg,
                    )
                    _attach_llm_trace(
                        payload,
                        request=request,
                        requested=bool(body.use_llm),
                        result=result,
                        run_id=run_id,
                    )
                    await queue.put({"event": "result", "envelope": payload})
                except TextPIIServiceError as exc:
                    await queue.put({"event": "error", "detail": str(exc)})
                except Exception as exc:  # pragma: no cover - defensive
                    logger.exception("gateway_scan_stream failed")
                    await queue.put({"event": "error", "detail": "scan failed"})
                finally:
                    await queue.put(done)

            task = asyncio.ensure_future(_run())
            try:
                while True:
                    if await request.is_disconnected():
                        task.cancel()
                        break
                    item = await queue.get()
                    if item is done:
                        break
                    yield (json.dumps(item) + "\n").encode("utf-8")
            finally:
                if not task.done():
                    task.cancel()
            logger.info(
                "gateway_scan_stream actor=%s chars=%s original=%s truncated=%s "
                "latency_ms=%.1f",
                _actor(request),
                len(text),
                original_n,
                truncated,
                (time.perf_counter() - t0) * 1000,
            )

        resp = StreamingResponse(_events(), media_type="application/x-ndjson")
        _no_store(resp)
        return resp

    @app.post("/api/gateway/guard")
    async def gateway_guard(request: Request, body: GatewayGuardBody) -> Any:
        """Standalone toxicity / prompt-injection check — no PII highlighting.

        Meant for pre-flight chat-message gating (e.g. before the CopilotKit
        planner). Runs heuristics for free; LLM judges only when configured.
        """
        text = (body.text or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="text is required")
        if not GUARD_LIMITER.allow(f"gateway-guard:{_actor(request)}"):
            raise HTTPException(
                status_code=429,
                detail="too many guard checks — wait a moment and try again",
            )
        clipped, original_n, truncated = _clip(text)
        cfg = _redibis_config()
        scan_body = GatewayScanBody(
            text=clipped,
            check_toxicity=body.check_toxicity,
            check_prompt_injection=body.check_prompt_injection,
        )
        with _gateway_llm_run() as run_id:
            guards = _run_guards(scan_body, clipped, cfg)
        from redibis.pii.text_guards import strongest_action

        action, reasons = strongest_action(guards, thresholds=_guard_thresholds(cfg))
        payload = {
            "text_meta": {
                "char_count": len(clipped),
                "original_char_count": original_n,
                "truncated": truncated,
            },
            "analysers": {name: g.to_dict() for name, g in guards.items()},
            "decision": {"action": action, "reasons": reasons},
            "run_uuid": run_id,
        }
        _attach_llm_trace(
            payload,
            request=request,
            requested=False,
            result=None,
            run_id=run_id,
        )
        logger.info(
            "gateway_guard actor=%s chars=%s checks=%s decision=%s",
            _actor(request),
            len(clipped),
            list(guards.keys()),
            action,
        )
        return _json_no_store(payload)

    @app.post("/api/gateway/suggest-policy")
    async def gateway_suggest_policy(
        request: Request, body: GatewaySuggestBody
    ) -> Any:
        text, original_n, truncated = _guard(request, body.text)
        policy = _call(
            _svc().suggest_policy,
            text,
            language=body.language,
            engines=body.engines,
            min_score=body.min_score,
        )
        data = policy.to_dict()
        data["id"] = body.policy_id or policy.id
        logger.info(
            "gateway_suggest_policy actor=%s chars=%s original=%s "
            "truncated=%s id=%s",
            _actor(request),
            len(text),
            original_n,
            truncated,
            data.get("id"),
        )
        return _json_no_store(data)

    @app.post("/api/gateway/deidentify")
    async def gateway_deidentify(request: Request, body: GatewayDeidBody) -> Any:
        from redibis.pii.deid.policy import DeidPolicy

        text, original_n, truncated = _guard(request, body.text)
        if not body.policy and not body.policy_id:
            raise HTTPException(
                status_code=400,
                detail="de-identification requires a reviewed policy",
            )
        policy = DeidPolicy.from_dict(body.policy) if body.policy else None
        result, deid = _call(
            _svc().deidentify,
            text,
            policy=policy,
            policy_id=body.policy_id,
            scan_kwargs=_scan_kwargs(body, UI_MAX_CHARS),
        )
        payload = envelope(
            result, truncated=truncated, original_char_count=original_n
        )
        payload["deidentified"] = _deidentified_block(deid)
        _strip_keys(payload)
        logger.info(
            "gateway_deidentify actor=%s chars=%s applied=%s policy=%s",
            _actor(request),
            len(text),
            len(payload["deidentified"].get("applied") or []),
            payload["deidentified"].get("policy_id"),
        )
        return _json_no_store(payload)
