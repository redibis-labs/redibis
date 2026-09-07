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
from typing import Any, Optional

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

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

KEY_FIELDS = (
    "master_key",
    "master_key_hex",
    "seed",
    "key",
    "keys",
    "run_key_ref",
    "run_key_ref",
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


def _json_no_store(payload: dict) -> JSONResponse:
    resp = JSONResponse(payload)
    _no_store(resp)
    return resp


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

    return {
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
    }


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
                "default_model_bare": p.get("default_model_bare"),
            }
            for p in list_providers()
        ]
    except Exception:
        return []


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

    @app.get("/api/gateway/health")
    async def gateway_health(request: Request) -> Any:
        """Truthful engine/provider availability — safe fields only (no keys)."""
        health = _call(_svc().health)
        cfg = _redibis_config()
        gw_cfg = getattr(cfg, "text_gateway", None)
        toxicity_llm_enabled = bool(getattr(gw_cfg, "toxicity_llm_enabled", False))
        prompt_injection_llm_enabled = bool(getattr(gw_cfg, "prompt_injection_llm_enabled", False))
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
            "llm_providers": _safe_llm_providers(),
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
            "default_llm": {
                "role": "pii.text_refiner",
                "provider": (health.get("engines") or {}).get("llm", {}).get("role_provider")
                or "",
                "role_bound": bool(
                    (health.get("engines") or {}).get("llm", {}).get("role_bound")
                ),
            },
        }
        return _json_no_store(payload)

    @app.post("/api/gateway/scan")
    async def gateway_scan(request: Request, body: GatewayScanBody) -> Any:
        text, original_n, truncated = _guard(request, body.text)
        t0 = time.perf_counter()
        cfg = _redibis_config()
        result = _call(_svc().scan, text, **_scan_kwargs(body, UI_MAX_CHARS))
        guards = _run_guards(body, text, cfg)
        payload = envelope(
            result,
            truncated=truncated,
            original_char_count=original_n,
            guards=guards,
            redibis_config=cfg,
        )
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
                return _svc().scan(
                    text, progress_cb=_on_stage, **_scan_kwargs(body, UI_MAX_CHARS)
                )

            async def _run():
                try:
                    result = await run_in_threadpool(_do_scan)
                    # Guards may call an external LLM (network I/O) when the
                    # matching text_gateway.*_llm_enabled flag is set — keep
                    # that off the event loop too, or the "streamed so it
                    # never blocks" endpoint blocks on exactly that call.
                    guards = await run_in_threadpool(_run_guards, body, text, cfg)
                    payload = envelope(
                        result,
                        truncated=truncated,
                        original_char_count=original_n,
                        guards=guards,
                        redibis_config=cfg,
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
        }
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
