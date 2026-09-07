"""Single entry point for RAI-guarded model calls."""

from __future__ import annotations

from contextlib import nullcontext
from typing import Any, Callable, Optional, TypeVar

from redibis.config import RAIConfig, RedibisConfig
from redibis.enrich.providers import EnrichmentProvider
from redibis.telemetry.pii_scope import infer_contains_raw_pii
from redibis.telemetry.rai import RAIMiddleware

T = TypeVar("T")

_LOCAL_API_MARKERS = ("localhost", "127.0.0.1", "0.0.0.0", "::1")
_CLOUD_PROVIDER_NAMES = frozenset({"claude", "gemini", "openai", "openrouter"})


def resolve_provider_residency(
    provider: EnrichmentProvider,
    *,
    rai_config: RAIConfig,
    override: str = "",
) -> str:
    explicit = (override or getattr(provider, "residency", "") or "").strip().lower()
    if explicit:
        return explicit

    api_base = (
        getattr(provider, "api_base", None) or getattr(provider, "endpoint_url", None) or ""
    ).lower()
    if api_base:
        if any(marker in api_base for marker in _LOCAL_API_MARKERS):
            return "local"
        return "private"

    name = (getattr(provider, "name", "") or "").strip().lower()
    if name in _CLOUD_PROVIDER_NAMES:
        return "public"

    default = (rai_config.default_residency or "local").strip().lower()
    return default or "local"


def guarded_model_call(
    fn: Callable[[], T],
    *,
    model_id: str,
    provider: Optional[EnrichmentProvider] = None,
    contract: Optional[dict[str, Any]] = None,
    table: str = "",
    user_prompt: str = "",
    system_prompt: str = "",
    detections: Optional[list[Any]] = None,
    residency: str = "",
    rai_config: Optional[RAIConfig] = None,
    redibis_config: Optional[RedibisConfig] = None,
    middleware: Optional[RAIMiddleware] = None,
    attested_masked_external: bool = False,
    model_role: str = "",
    routing_revision: int = 0,
    run_id: str = "",
    context: Optional[dict[str, Any]] = None,
    parent_call_id: str = "",
    step_id: str = "",
    attempt: int = 1,
    node_id: str = "",
    node_kind: str = "",
) -> tuple[T, Optional[dict[str, Any]]]:
    """Run ``fn`` behind RAI checks; return ``(result, rai_report)``."""
    from redibis.enrich.llm_logging import llm_call_context
    from redibis.telemetry.llm_evidence import current_llm_evidence_recorder, persist_guarded_call

    cfg = rai_config or (redibis_config.rai if redibis_config else RAIConfig())
    ctx = llm_call_context(
        model_role=model_role,
        routing_revision=routing_revision,
        run_id=run_id,
    ) if (model_role or routing_revision or run_id) else nullcontext()

    result: Any = None
    report: Optional[dict[str, Any]] = None
    status = "ok"
    error = ""
    blocked = False
    with ctx:
        try:
            if not cfg.enabled:
                result = fn()
                return result, None

            rai = middleware or RAIMiddleware(cfg)
            contains_raw_pii, pii_columns = infer_contains_raw_pii(
                contract=contract,
                table=table,
                user_prompt=user_prompt,
                detections=detections,
                attested_masked_external=attested_masked_external,
            )
            effective_residency = resolve_provider_residency(
                provider,
                rai_config=cfg,
                override=residency,
            ) if provider is not None else (residency or cfg.default_residency or "local")

            result = rai.wrap_model_call(
                fn,
                model_id=model_id,
                residency=effective_residency,
                contains_raw_pii=contains_raw_pii,
                pii_columns=pii_columns,
            )
            report = rai.report()
            report["residency"] = effective_residency
            report["contains_raw_pii"] = contains_raw_pii
            report["pii_columns"] = pii_columns
            report["attested_masked_external"] = attested_masked_external
            if model_role:
                report["model_role"] = model_role
            if routing_revision:
                report["routing_revision"] = int(routing_revision)
            if run_id:
                report["run_id"] = run_id
            blocked = bool(any(d.get("blocked") for d in (report.get("decisions") or [])))
            return result, report
        except PermissionError as exc:
            status = "blocked"
            blocked = True
            error = str(exc)
            raise
        except Exception as exc:
            status = "error"
            error = str(exc)
            raise
        finally:
            response_text = result if isinstance(result, str) else (
                "" if result is None else str(result)
            )
            try:
                persist_guarded_call(
                    model_id=model_id,
                    provider=provider,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    response_text=response_text,
                    parsed_result=None if isinstance(result, str) else result,
                    rai_report=report,
                    status=status,
                    error=error,
                    blocked=blocked,
                    context=context,
                    model_role=model_role,
                    run_id=run_id,
                    table=table,
                    parent_call_id=parent_call_id,
                    step_id=step_id,
                    attempt=attempt,
                    node_id=node_id,
                    node_kind=node_kind,
                )
            except Exception as persist_exc:
                import logging as _logging
                _logging.getLogger(__name__).warning(
                    "LLM evidence persist failed model_id=%s run_id=%s: %s",
                    model_id, run_id, persist_exc,
                )
                try:
                    rec = current_llm_evidence_recorder()
                    if rec is not None:
                        rec.warn("persist", f"{type(persist_exc).__name__}: {persist_exc}")
                except Exception:
                    _logging.getLogger(__name__).warning(
                        "LLM evidence warning record also failed",
                    )
