"""Bounded LLM connectivity probe — shared by CLI and Settings → LLM test."""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

from redibis.enrich.llm_logging import redact
from redibis.enrich.providers import get_provider
from redibis.services.run_logging import capture_run_log

# User typo / shorthand → registry name
_PROVIDER_ALIASES = {
    "slang": "sglang",
    "sg-lang": "sglang",
}


def normalize_provider_name(name: str) -> str:
    key = (name or "").strip().lower()
    return _PROVIDER_ALIASES.get(key, key)


def probe_provider(
    name: str,
    *,
    model: str = "",
    api_key: Optional[str] = None,
    endpoint_url: Optional[str] = None,
    prompt: Optional[str] = None,
    timeout: float = 20.0,
    providers_file: Optional[str] = None,
) -> dict[str, Any]:
    """Run a tiny chat completion against one registered provider.

    Returns a dict with ``ok``, ``provider``, ``model``, ``latency_ms``,
    ``response_snippet``, ``error``, ``log`` (redacted LiteLLM debug tail).
    Never persists API keys.
    """
    key = normalize_provider_name(name)
    user_prompt = (prompt or "").strip() or "Reply with exactly: OK"
    max_tokens = 128 if (prompt or "").strip() else 16
    timeout = max(1.0, min(float(timeout or 20.0), 60.0))

    try:
        provider = get_provider(
            key,
            model=model or "",
            api_key=api_key,
            endpoint_url=endpoint_url,
            config_path=providers_file,
            timeout=timeout,
            max_tokens=max_tokens,
        )
    except Exception as exc:
        return {
            "ok": False,
            "stage": "config",
            "provider": key,
            "model": model or "",
            "latency_ms": 0,
            "response_snippet": "",
            "error": str(exc),
            "log": "",
        }

    key_secrets = [getattr(provider, "api_key", None) or api_key or ""]
    t0 = time.perf_counter()
    litellm_logger = logging.getLogger("LiteLLM")
    prev_level = litellm_logger.level
    litellm_logger.setLevel(logging.DEBUG)
    ok = False
    err = ""
    out = ""
    log_tail = ""
    try:
        with capture_run_log() as buf:
            try:
                from redibis.config import RedibisConfig
                from redibis.telemetry.llm_evidence import llm_evidence_recorder, new_llm_run_id
                from redibis.telemetry.model_gateway import guarded_model_call

                cfg_path = os.environ.get("REDIBIS_CONFIG")
                cfg = RedibisConfig.from_yaml(cfg_path) if cfg_path else RedibisConfig.default()
                probe_run_id = new_llm_run_id("provider-probe")

                def _invoke() -> str:
                    return provider.complete(
                        "You are a connectivity probe.",
                        user_prompt,
                        json_mode=False,
                    )

                with llm_evidence_recorder(
                    run_id=probe_run_id,
                    table="",
                    execution_mode="single_llm",
                    config=cfg,
                ):
                    out, _rai = guarded_model_call(
                        _invoke,
                        model_id=str(
                            getattr(provider, "_effective_model", lambda: "")()
                            or getattr(provider, "model", "")
                            or key
                        ),
                        provider=provider,
                        user_prompt=user_prompt,
                        system_prompt="You are a connectivity probe.",
                        redibis_config=cfg,
                        model_role="provider.probe",
                        run_id=probe_run_id,
                    )
                ok, err = True, ""
            except Exception as exc:
                ok, err, out = False, str(exc), ""
            log_tail = redact(buf.getvalue(), secrets=key_secrets)[-4000:]
    finally:
        litellm_logger.setLevel(prev_level)

    model_used = getattr(provider, "litellm_model", "") or model or ""
    if model and hasattr(provider, "_effective_model"):
        try:
            model_used = provider._effective_model()
        except Exception:
            model_used = model

    return {
        "ok": ok,
        "provider": key,
        "model": model_used,
        "latency_ms": round((time.perf_counter() - t0) * 1000),
        "response_snippet": (out or "")[:500],
        "error": redact(err, secrets=key_secrets),
        "log": log_tail,
    }
