"""Structured logging + in-memory ring buffer for LiteLLM enrichment calls."""

from __future__ import annotations

import contextvars
import hashlib
import logging
import os
import re
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import asdict
from threading import Lock
from typing import Any, Iterable, Iterator, Optional
from urllib.parse import urlparse

from redibis.telemetry.models import ModelCallRecord

_llm_log = logging.getLogger("redibis.llm")

_RING: deque[ModelCallRecord] = deque(maxlen=200)
_RING_LOCK = Lock()

# Propagated from guarded_model_call into provider.complete recording.
_CALL_CTX: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "redibis_llm_call_ctx",
    default={},
)


@contextmanager
def llm_call_context(
    *,
    model_role: str = "",
    routing_revision: int = 0,
    run_id: str = "",
) -> Iterator[None]:
    """Attach role/revision/run metadata to LLM call records for this stack."""
    prev = _CALL_CTX.get({})
    token = _CALL_CTX.set({
        **prev,
        "model_role": model_role or prev.get("model_role") or "",
        "routing_revision": int(routing_revision or prev.get("routing_revision") or 0),
        "run_id": run_id or prev.get("run_id") or "",
    })
    try:
        yield
    finally:
        _CALL_CTX.reset(token)


def current_llm_call_context() -> dict[str, Any]:
    return dict(_CALL_CTX.get({}) or {})

_REDACT_PATTERNS = (
    (re.compile(r"sk-[A-Za-z0-9_-]{8,}", re.I), "sk-[REDACTED]"),
    (re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+", re.I), "Bearer [REDACTED]"),
    (re.compile(r"api_key\s*[=:]\s*['\"]?[^'\"\s,}]+", re.I), "api_key=[REDACTED]"),
    (re.compile(r"authorization\s*[=:]\s*['\"]?[^'\"\s,}]+", re.I), "authorization=[REDACTED]"),
    (re.compile(r"AIza[0-9A-Za-z_\-]{20,}"), "AIza[REDACTED]"),
    (re.compile(r"\b(sk-ant|xai|gsk)[A-Za-z0-9_\-]{8,}", re.I), "[REDACTED]"),
    (re.compile(r"x-api-key\s*[=:]\s*['\"]?[^'\"\s,}]+", re.I), "x-api-key=[REDACTED]"),
    (re.compile(r"api-key\s*[=:]\s*['\"]?[^'\"\s,}]+", re.I), "api-key=[REDACTED]"),
)


def redact(text: str, *, secrets: Iterable[str] = ()) -> str:
    """Strip API keys and auth headers from captured log text."""
    out = text or ""
    for s in secrets:
        if s and len(s) >= 6:
            out = out.replace(s, "[REDACTED]")
    for pattern, repl in _REDACT_PATTERNS:
        out = pattern.sub(repl, out)
    return out


def _host_only(api_base: Optional[str]) -> str:
    if not api_base:
        return ""
    try:
        parsed = urlparse(api_base)
        if parsed.netloc:
            return parsed.netloc
        return api_base.split("?")[0][:120]
    except Exception:
        return str(api_base)[:120]


def is_llm_debug_enabled() -> bool:
    if os.getenv("REDIBIS_LLM_DEBUG", "").strip().lower() in ("1", "true", "yes"):
        return True
    try:
        from redibis.config import RedibisConfig

        cfg_path = os.environ.get("REDIBIS_CONFIG")
        cfg = RedibisConfig.from_yaml(cfg_path) if cfg_path else RedibisConfig.default()
        return bool(cfg.observability.llm_debug)
    except Exception:
        return False


def is_llm_log_prompts_enabled() -> bool:
    if os.getenv("REDIBIS_LLM_LOG_PROMPTS", "").strip().lower() in ("1", "true", "yes"):
        return True
    try:
        from redibis.config import RedibisConfig

        cfg_path = os.environ.get("REDIBIS_CONFIG")
        cfg = RedibisConfig.from_yaml(cfg_path) if cfg_path else RedibisConfig.default()
        return bool(cfg.observability.llm_log_prompts)
    except Exception:
        return False


def _extract_usage(resp: Any) -> tuple[int, int, float]:
    prompt_tokens = completion_tokens = 0
    cost_usd = 0.0
    if resp is None:
        return prompt_tokens, completion_tokens, cost_usd
    usage = getattr(resp, "usage", None)
    if usage is None and isinstance(resp, dict):
        usage = resp.get("usage")
    if usage is not None:
        if isinstance(usage, dict):
            prompt_tokens = int(usage.get("prompt_tokens") or 0)
            completion_tokens = int(usage.get("completion_tokens") or 0)
        else:
            prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
            completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    try:
        import litellm

        cost_usd = float(litellm.completion_cost(completion_response=resp) or 0.0)
    except Exception:
        pass
    return prompt_tokens, completion_tokens, cost_usd


def build_model_call_record(
    provider: Any,
    model: str,
    resp: Any,
    *,
    latency_ms: float,
    status: str,
    error: str = "",
    api_base: Optional[str] = None,
    system_prompt_len: int = 0,
    user_prompt_len: int = 0,
    error_secrets: Iterable[str] = (),
    model_role: str = "",
    routing_revision: int = 0,
    run_id: str = "",
) -> ModelCallRecord:
    prompt_tokens, completion_tokens, cost_usd = _extract_usage(resp)
    ctx = current_llm_call_context()
    return ModelCallRecord(
        model_id=model or getattr(provider, "litellm_model", "") or getattr(provider, "model", ""),
        provider=getattr(provider, "name", "unknown"),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_ms=round(latency_ms, 2),
        cost_usd=cost_usd,
        residency=getattr(provider, "residency", "") or "local",
        status=status,
        api_base=_host_only(api_base or getattr(provider, "api_base", None) or getattr(provider, "endpoint_url", None)),
        error=redact(error, secrets=error_secrets)[:500],
        system_prompt_len=system_prompt_len,
        user_prompt_len=user_prompt_len,
        model_role=model_role or str(ctx.get("model_role") or ""),
        routing_revision=int(routing_revision or ctx.get("routing_revision") or 0),
        run_id=run_id or str(ctx.get("run_id") or ""),
    )


def record_llm_call(rec: ModelCallRecord) -> None:
    with _RING_LOCK:
        _RING.append(rec)
    try:
        from redibis.telemetry.otel import get_active_collector

        collector = get_active_collector()
        if collector is not None:
            collector.record_model_call(rec)
    except Exception:
        pass


def get_recent_llm_calls(
    *,
    limit: int = 50,
    model_role: str = "",
    run_id: str = "",
    routing_revision: Optional[int] = None,
) -> list[dict]:
    lim = max(1, min(int(limit or 50), 200))
    role_f = (model_role or "").strip()
    run_f = (run_id or "").strip()
    with _RING_LOCK:
        items = list(_RING)
    out: list[dict] = []
    for rec in reversed(items):
        if role_f and (rec.model_role or "") != role_f:
            continue
        if run_f and (rec.run_id or "") != run_f:
            continue
        if routing_revision is not None and int(rec.routing_revision or 0) != int(routing_revision):
            continue
        out.append(asdict(rec))
        if len(out) >= lim:
            break
    return out


def emit_llm_log(rec: ModelCallRecord, *, error: str = "", error_secrets: Iterable[str] = ()) -> None:
    payload = asdict(rec)
    _llm_log.info(
        "LLM call provider=%s model=%s status=%s latency_ms=%.0f tokens=%s/%s cost=%.4f",
        rec.provider,
        rec.model_id,
        rec.status,
        rec.latency_ms,
        rec.prompt_tokens,
        rec.completion_tokens,
        rec.cost_usd,
        extra={"llm_call": payload},
    )
    if error:
        _llm_log.info("LLM call error provider=%s model=%s: %s", rec.provider, rec.model_id, redact(error, secrets=error_secrets))


def prompt_meta(system_prompt: str, user_prompt: str) -> dict[str, Any]:
    sys_len = len(system_prompt or "")
    usr_len = len(user_prompt or "")
    meta: dict[str, Any] = {
        "system_prompt_len": sys_len,
        "user_prompt_len": usr_len,
    }
    if is_llm_log_prompts_enabled():
        combined = f"{system_prompt}\n{user_prompt}"
        meta["prompt_hash"] = hashlib.sha256(combined.encode("utf-8")).hexdigest()[:16]
    return meta
