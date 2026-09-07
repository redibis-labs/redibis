"""
Webapp — FastAPI backend for the redibis dashboard.

This layer is a THIN adapter over the service layer. All real logic lives in
``redibis.services`` (session_service, scan_service, discovery_service) and the
``redibis.store.config_store`` named-list registry. Heavy imports (great_expectations
etc.) are kept lazy so the module imports cleanly in minimal environments.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from contextlib import asynccontextmanager
from uuid import uuid4
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import yaml

logger = logging.getLogger("redibis.webapp")

from fastapi import Depends, FastAPI, HTTPException, UploadFile, File, Form, Request, BackgroundTasks, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from redibis.store.auth_store import apply_scoped_edits, SCOPES
from redibis.store.subcontract_store import VALID_KINDS
from redibis.config import ConfigError
from redibis.webapp.auth_deps import require_admin
from redibis.webapp.jobs import ScanQueueFullError, submit_scan_job
from redibis.webapp.resilience import DependencyTimeoutError, call_with_timeout
from redibis.webapp.security import (
    AuthMiddleware,
    HttpsRedirectMiddleware,
    LOGIN_MESSAGE,
    csrf_from_request,
    csrf_ok,
    login_allowed,
    session_token,
    set_session_cookie,
    clear_session_cookie,
    start_auth_warnings,
    stop_auth_warnings,
)
from redibis.webapp.store_accessors import (
    backend_ping,
    clear_stores,
    get_auth_store,
    get_backend_store,
    get_config_store,
    get_contract_store,
    get_contracts_bucket,
    get_run_merger,
    get_runs_bucket,
    get_subcontract_store,
    load_global_settings,
    memory_ready,
    store_op,
)

if TYPE_CHECKING:
    from redibis.agents.dynamic_tools import DynamicToolRegistry
    from redibis.agents.lineage_store import LineageStore
    from redibis.agents.tool_runner import ToolContext


def _redibis_config():
    """Root config spine for RAI, classification, agents, etc."""
    from redibis.config import RedibisConfig
    cfg_path = os.environ.get("REDIBIS_CONFIG")
    return RedibisConfig.from_yaml(cfg_path) if cfg_path else RedibisConfig.default()


@asynccontextmanager
async def _lifespan(app: FastAPI):
    try:
        from redibis.obs import setup_logging
        from redibis.telemetry.init import init_otel

        cfg = _redibis_config()
        obs = cfg.observability
        setup_logging(
            level=obs.log_level,
            fmt=obs.log_format or None,
            module_levels=obs.module_levels,
            decision_log=obs.decision_log,
        )
        init_otel(cfg)
        agents = cfg.agents
        if agents.enabled and (
            agents.auto_approve_writes
            or agents.dynamic_sandbox_enabled
            or agents.allow_external_codegen
        ):
            logger.warning(
                "agents open defaults active (auto_approve_writes=%s, "
                "dynamic_sandbox_enabled=%s, allow_external_codegen=%s, "
                "copilotkit_enabled=%s) — bind to loopback for local use or "
                "set these false in REDIBIS_CONFIG for shared hosts",
                agents.auto_approve_writes,
                agents.dynamic_sandbox_enabled,
                agents.allow_external_codegen,
                agents.copilotkit_enabled,
            )
    except Exception:
        logger.warning("observability startup skipped", exc_info=True)
    start_auth_warnings()
    try:
        get_auth_store()
    except ConfigError:
        raise
    except ValueError:
        raise
    except Exception:
        logger.error("auth bootstrap FAILED — no admin may exist", exc_info=True)
    yield
    stop_auth_warnings()
    from redibis.webapp.jobs import shutdown_jobs

    shutdown_jobs(wait=False)


app = FastAPI(title="Redibis Dashboard", version="1.0.0", lifespan=_lifespan)


# Last add_middleware is outermost. CORS must wrap Auth so 401/403 still carry
# Access-Control-Allow-Origin when REDIBIS_CORS_ORIGINS is set.
_cors_origins = [o.strip() for o in os.getenv("REDIBIS_CORS_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(AuthMiddleware)
app.add_middleware(HttpsRedirectMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=bool(_cors_origins),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _request_id(request: Request, call_next):
    cid = request.headers.get("x-request-id") or uuid4().hex[:12]
    from redibis.obs import bind_context

    with bind_context(run_id=cid, fn=request.url.path):
        resp = await call_next(request)
    resp.headers["x-request-id"] = cid
    return resp


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    cid = request.headers.get("x-request-id") or uuid4().hex[:12]
    logger.exception("unhandled %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"error": "internal_error", "correlation_id": cid},
        headers={"x-request-id": cid},
    )


@app.exception_handler(DependencyTimeoutError)
async def _dependency_timeout(request: Request, exc: DependencyTimeoutError):
    return JSONResponse(
        status_code=503,
        content={"error": "dependency_timeout", "detail": str(exc)},
    )


_STORE_TIMEOUT_SEC = float(os.getenv("REDIBIS_STORE_TIMEOUT_SEC", "30"))
_READY_TIMEOUT_SEC = float(os.getenv("REDIBIS_READY_TIMEOUT_SEC", "5"))

_WEBAPP_DIR = Path(__file__).parent
_STATIC_DIR = _WEBAPP_DIR / "static"
_TEMPLATES_DIR = _WEBAPP_DIR / "templates"

if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

_TEMPLATES = Jinja2Templates(directory=str(_TEMPLATES_DIR))
_TEMPLATES.env.auto_reload = True


def _page_context(request: Request, **extra) -> dict:
    user = getattr(request.state, "user", None)
    ctx = {
        "request": request,
        "user": user,
        "csrf_token": getattr(request.state, "csrf_token", "") or "",
    }
    ctx.update(extra)
    return ctx


def _agents_enabled() -> bool:
    return bool(_redibis_config().agents.enabled)


def _require_agents() -> None:
    if not _agents_enabled():
        raise HTTPException(
            status_code=403,
            detail="agents.enabled is false — set agents.enabled: true in REDIBIS_CONFIG",
        )


def _scan_output_dir() -> Path:
    """Read SCAN_OUTPUT_DIR at call time (tests set env before first request)."""
    root = Path(os.getenv("SCAN_OUTPUT_DIR", "./scan_output"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def _configs_dir() -> Path:
    return Path(
        os.getenv("REDIBIS_CONFIGS_DIR")
        or os.getenv("CONFIGS_DIR")
        or "./configs"
    )


# UI timeouts (seconds) — persisted in global_settings.ui_prefs; defaults doubled from v0.
DEFAULT_SCAN_WAIT_TIMEOUT_SEC = 360
DEFAULT_SSE_PING_TIMEOUT_SEC = 30.0


def _ui_timeouts() -> dict[str, float]:
    """Load scan-wait and SSE ping timeouts from persisted UI preferences."""
    gs = load_global_settings()
    ui = gs.get("ui_prefs") if isinstance(gs.get("ui_prefs"), dict) else {}
    scan_raw = ui.get("scan_wait_timeout_sec", DEFAULT_SCAN_WAIT_TIMEOUT_SEC)
    sse_raw = ui.get("sse_ping_timeout_sec", DEFAULT_SSE_PING_TIMEOUT_SEC)
    try:
        scan_sec = max(60, min(int(scan_raw), 7200))
    except (TypeError, ValueError):
        scan_sec = DEFAULT_SCAN_WAIT_TIMEOUT_SEC
    try:
        sse_sec = max(5.0, min(float(sse_raw), 300.0))
    except (TypeError, ValueError):
        sse_sec = DEFAULT_SSE_PING_TIMEOUT_SEC
    return {"scan_wait_timeout_sec": scan_sec, "sse_ping_timeout_sec": sse_sec}


def _global_llm_defaults() -> dict[str, Any]:
    gs = load_global_settings()
    raw = gs.get("llm_defaults") if isinstance(gs, dict) else {}
    llm = raw if isinstance(raw, dict) else {}
    return {
        "provider": str(llm.get("provider") or "").strip(),
        "model": str(llm.get("model") or "").strip(),
        "endpoint_url": str(llm.get("endpoint_url") or "").strip(),
        "enabled": bool(llm.get("enabled")),
    }


def _default_llm_provider() -> str:
    """Resolve the default enrichment/planner provider from config (overridable for air-gap)."""
    llm = _global_llm_defaults()
    if llm.get("provider"):
        return llm["provider"]
    cfg = _redibis_config()
    prov = (cfg.pii.llm.provider or "").strip()
    if prov:
        return prov
    return "gemini"


def _global_agentic_defaults() -> dict[str, str]:
    gs = load_global_settings()
    raw = gs.get("agentic_defaults") if isinstance(gs, dict) else {}
    agentic = raw if isinstance(raw, dict) else {}
    llm = _global_llm_defaults()
    mode = str(agentic.get("planner_mode") or "auto").strip().lower()
    if mode == "heuristic":
        return {
            "planner_mode": "heuristic",
            "planner_provider": "",
            "planner_model": "",
        }
    agentic_provider = str(agentic.get("planner_provider") or "").strip()
    if agentic_provider:
        return {
            "planner_mode": "llm",
            "planner_provider": agentic_provider,
            "planner_model": str(agentic.get("planner_model") or "").strip(),
        }
    return {
        "planner_mode": "auto",
        "planner_provider": str(llm.get("provider") or "").strip(),
        "planner_model": str(llm.get("model") or "").strip(),
    }


def _resolved_planner_settings() -> dict[str, str]:
    """Resolve the effective planner once and report where it came from."""
    cfg = _redibis_config()
    provider = str(cfg.agents.planner_provider or "").strip()
    model = str(cfg.agents.planner_model or "").strip()
    source = "redibis_config" if provider else ""
    if not provider:
        global_agentic = _global_agentic_defaults()
        if global_agentic.get("planner_mode") == "heuristic":
            return {
                "method": "heuristic",
                "provider": "",
                "model": "",
                "source": "global_settings",
            }
        provider = global_agentic.get("planner_provider", "")
        model = global_agentic.get("planner_model", "") if provider else ""
        source = "global_settings" if provider else ""
    if not provider:
        try:
            from redibis.agents.run_defaults import load_defaults

            planner = load_defaults().get("planner") or {}
        except Exception:
            planner = {}
        provider = str(planner.get("provider") or "").strip()
        model = str(planner.get("model") or "").strip() if provider else ""
        source = "run_defaults" if provider else ""
    return {
        "method": "llm" if provider else "heuristic",
        "provider": provider,
        "model": model,
        "source": source or "built_in",
    }


def _config_with_resolved_planner():
    """Return request-local config with hot planner defaults applied."""
    cfg = _redibis_config()
    resolved = _resolved_planner_settings()
    cfg.agents.planner_provider = resolved["provider"]
    cfg.agents.planner_model = resolved["model"]
    return cfg


def _provider_for_role(
    role: str,
    *,
    provider: str = "",
    model: str = "",
    api_key: Optional[str] = None,
    endpoint_url: Optional[str] = None,
):
    """Resolve an EnrichmentProvider via capability routing (request overrides win)."""
    from redibis.enrich.capability_routing import RoutingError, get_provider_for_role

    try:
        return get_provider_for_role(
            role,
            gs=get_config_store().load_global_settings(),
            agents_cfg=_redibis_config().agents,
            override_provider=provider or "",
            override_model=model or "",
            api_key=api_key,
            endpoint_url=endpoint_url,
        )
    except RoutingError as exc:
        # Fall back to legacy get_provider when an explicit provider was requested.
        if (provider or "").strip():
            from redibis.enrich.providers import get_provider

            return get_provider(
                provider, model=model or "", api_key=api_key, endpoint_url=endpoint_url,
            ), None
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _require_session(session_id: str):
    from redibis.services.session.loader import resolve_session
    session = resolve_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


# ═══════════════════════════════════════════════════════════════════════════
# Sessions
# ═══════════════════════════════════════════════════════════════════════════

@app.post("/api/sessions")
async def create_session(
    file: UploadFile = File(...),
    table: str = Form("data.uploaded"),
    scan_mode: str = Form("both"),
    equation: str = Form("independent"),
    pii_engines: str = Form("both"),
    pii_regex_confidence: float = Form(0.80),
    pii_gliner_confidence: float = Form(0.40),
    pii_llm_confidence: float = Form(0.82),
    pii_gliner_model: str = Form(""),
    pii_models_dir: str = Form(""),
    pii_gliner_always_run: bool = Form(False),
    selected_columns: Optional[str] = Form(None),
    pii_regex_config: Optional[str] = Form(None),
    quality_config: Optional[str] = Form(None),
    auto_merge_contract: bool = Form(False),
    automerge: str = Form("none"),
) -> dict:
    from redibis.services.session_service import session_manager, GlobalConfig
    from redibis.pii.regex_overrides import RegexSet
    from redibis.quality.rule_set import QualityRuleSet

    file_bytes = await file.read()
    cols = json.loads(selected_columns) if selected_columns else None
    masking_default_locale = _redibis_config().masking.default_locale
    cc = GlobalConfig(
        scan_mode=scan_mode, equation_mode=equation, pii_engines=pii_engines,
        pii_regex_confidence=pii_regex_confidence, pii_gliner_confidence=pii_gliner_confidence,
        pii_llm_confidence=pii_llm_confidence, pii_gliner_model=pii_gliner_model,
        pii_models_dir=pii_models_dir.strip(),
        pii_gliner_always_run=pii_gliner_always_run, selected_columns=cols,
        masking_default_locale=masking_default_locale,
        auto_merge_contract=auto_merge_contract, automerge=automerge,
    )
    # Optionally seed the embedded lists from saved named configs.
    if pii_regex_config:
        try:
            cc.regex_set = RegexSet.from_overrides(
                get_config_store().load_regex_config(pii_regex_config), name=pii_regex_config)
        except Exception:
            pass
    if quality_config:
        try:
            cc.quality_rule_set = QualityRuleSet(
                name=quality_config, rules=get_config_store().load_quality_config(quality_config))
        except Exception:
            pass

    _seed_pii_ner_labels_from_global(cc)

    session = session_manager.create_session(table, file_bytes, _scan_output_dir(), common_config=cc)
    # Persist auto-resolved NER path (sole model under /models) into session config.
    resolved_ner = cc.pii_gliner_model.strip()
    if not resolved_ner:
        from redibis.services.session.config import _effective_ner_model_path
        resolved_ner = _effective_ner_model_path(cc)
        if resolved_ner:
            cc.pii_gliner_model = resolved_ner
            from pathlib import Path
            cc.active_ner_model_name = Path(resolved_ner).name
            session.common_config = cc
            session.persist_to_disk()
    return {"session_id": session.session_id, "status": session.status,
            "common_config": cc.to_dict()}


@app.get("/api/sessions")
async def list_sessions() -> list:
    from redibis.services.session_service import session_manager
    return session_manager.list_sessions(_scan_output_dir())


@app.get("/api/session-sources")
def session_sources() -> list[dict]:
    from redibis.services.session.sources import list_sources

    return [{"name": s.name, "label": s.label, "root": str(s.root())} for s in list_sources()]


@app.get("/api/sessions/available")
def sessions_available(source: str = "scan") -> dict:
    from redibis.services.session import loader as load_module

    try:
        return {"source": source, "sessions": load_module.list_available(source)}
    except KeyError:
        raise HTTPException(status_code=400, detail=f"Unknown source {source!r}")


class LoadSessionBody(BaseModel):
    run_id: str
    source: str = "scan"


@app.post("/api/sessions/load")
def load_session_route(body: LoadSessionBody) -> dict:
    from redibis.services.session import loader as load_module

    try:
        return load_module.load_session(body.run_id, body.source)
    except FileNotFoundError:
        raise HTTPException(
            status_code=404,
            detail=f"No session {body.run_id!r} under source {body.source!r}",
        )
    except KeyError:
        raise HTTPException(status_code=400, detail=f"Unknown source {body.source!r}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/sessions/{session_id}")
async def get_session_state(session_id: str) -> dict:
    session = _require_session(session_id)
    session.prepare_for_api()
    return session.to_dict()


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str) -> dict:
    from redibis.services.session_service import session_manager
    ok = session_manager.delete_session(session_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"status": "deleted", "session_id": session_id}


@app.get("/api/sessions/{session_id}/sample")
async def get_sample(session_id: str, rows: int = 10) -> dict:
    return _require_session(session_id).get_sample(rows=rows)


@app.get("/api/sessions/{session_id}/stream")
async def stream_session(session_id: str, request: Request):
    from redibis.services.session_service import sse_log_generator
    session = _require_session(session_id)
    timeouts = _ui_timeouts()
    return StreamingResponse(
        sse_log_generator(
            session,
            ping_timeout=timeouts["sse_ping_timeout_sec"],
            request=request,
        ),
        media_type="text/event-stream",
    )


def _start_scan_job(session, fn, *args, **kwargs) -> None:
    try:
        submit_scan_job(fn, *args, session=session, **kwargs)
    except ScanQueueFullError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc


# ── The ONE scan button ──────────────────────────────────────────────────────

@app.post("/api/sessions/{session_id}/scan")
async def run_scan(session_id: str) -> dict:
    """Single entry point: reads the session's global config and decides behavior."""
    from redibis.services.session_service import execute_unified_scan
    session = _require_session(session_id)
    _start_scan_job(
        session,
        execute_unified_scan,
        session,
        get_backend_store(),
        get_contract_store(),
    )
    return {"status": "started", "session_id": session_id,
            "scan_mode": session.common_config.scan_mode}


# ── Per-step scan actions (quality workflow: profile → review → quality → pii) ─

def _parse_json_form(value: Optional[str]) -> Optional[list]:
    if not value:
        return None
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else None
    except json.JSONDecodeError:
        return None


@app.post("/api/sessions/{session_id}/step/profile")
async def run_profile_step(
    session_id: str,
    selected_columns: Optional[str] = Form(None),
) -> dict:
    from redibis.services.session_service import execute_profile_step
    from redibis.services.session.config import _build_scan_config

    session = _require_session(session_id)
    cols = _parse_json_form(selected_columns)
    if cols:
        session.common_config.selected_columns = cols
    session.config = _build_scan_config(
        session.table_name,
        session.common_config,
        Path(session.data_path).parent.parent,
    )

    _start_scan_job(
        session,
        execute_profile_step,
        session,
        session.config,
        get_contract_store(),
    )
    return {"status": "started", "step": "profile", "session_id": session_id}


@app.post("/api/sessions/{session_id}/step/quality")
async def run_quality_step(session_id: str) -> dict:
    from redibis.services.session_service import execute_quality_step

    session = _require_session(session_id)
    _start_scan_job(
        session,
        execute_quality_step,
        session,
        get_backend_store(),
        get_contract_store(),
    )
    return {"status": "started", "step": "quality", "session_id": session_id}


@app.post("/api/sessions/{session_id}/step/pii")
async def run_pii_step(
    session_id: str,
    pii_columns: Optional[str] = Form(None),
) -> dict:
    from redibis.services.session_service import execute_pii_step

    session = _require_session(session_id)
    cols = _parse_json_form(pii_columns)
    if cols:
        session.common_config.selected_columns = cols

    _start_scan_job(
        session,
        execute_pii_step,
        session,
        get_backend_store(),
        get_contract_store(),
    )
    return {"status": "started", "step": "pii", "session_id": session_id}


class QualityTemplateBody(BaseModel):
    template_name: str


@app.post("/api/sessions/{session_id}/draft/quality/template")
async def load_quality_template(session_id: str, body: QualityTemplateBody) -> dict:
    from redibis.quality.rule_set import QualityRuleSet

    session = _require_session(session_id)
    try:
        doc = get_config_store().load_quality_config_doc(body.template_name)
    except FileNotFoundError:
        raise HTTPException(
            status_code=404,
            detail=f"Quality template '{body.template_name}' not found",
        )
    session.common_config.quality_rule_set = QualityRuleSet(
        name=body.template_name, rules=doc.get("rules", []),
    )
    session.persist_to_disk()
    out = session.common_config.quality_rule_set.to_dict()
    if doc.get("ge_code"):
        out["ge_code"] = doc["ge_code"]
    return out


@app.post("/api/sessions/{session_id}/draft/quality/upload")
async def upload_quality_draft(session_id: str, file: UploadFile = File(...)) -> dict:
    from redibis.quality.rule_set import QualityRuleSet

    session = _require_session(session_id)
    raw = await file.read()
    try:
        data = yaml.safe_load(raw.decode("utf-8"))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid YAML: {e}")
    if isinstance(data, list):
        rules = data
        name = file.filename or "uploaded"
    elif isinstance(data, dict):
        rules = data.get("rules", [])
        name = data.get("name") or file.filename or "uploaded"
    else:
        raise HTTPException(status_code=400, detail="YAML must be a rule list or {rules: [...]}")
    if not rules:
        raise HTTPException(status_code=400, detail="No quality rules found in upload")
    session.common_config.quality_rule_set = QualityRuleSet(name=name, rules=list(rules))
    session.persist_to_disk()
    return session.common_config.quality_rule_set.to_dict()


@app.get("/api/sessions/{session_id}/artifacts/{key}")
async def get_artifact(session_id: str, key: str):
    from redibis.services.session.loader import artifact_path_allowed

    session = _require_session(session_id)
    session.prepare_for_api()
    path = session.artifacts.get(key)
    if not path:
        raise HTTPException(status_code=404, detail=f"Artifact '{key}' not found")
    artifact = Path(path)
    if not artifact.is_file():
        raise HTTPException(status_code=404, detail=f"Artifact '{key}' not found")
    session_dir = Path(session.data_path).parent
    if not artifact_path_allowed(artifact, session_dir=session_dir):
        raise HTTPException(status_code=404, detail=f"Artifact '{key}' not found")
    return FileResponse(artifact)


# ═══════════════════════════════════════════════════════════════════════════
# Data tab — preview, active columns, masking plan, de-identified export
# ═══════════════════════════════════════════════════════════════════════════

def _masking_service(session_id: str):
    from redibis.services.masking_service import MaskingService
    return MaskingService(_require_session(session_id))


class ColumnToggleBody(BaseModel):
    column: str
    include_in_scan: bool


class MaskPlanBody(BaseModel):
    columns: Optional[list[dict]] = None
    seed: Optional[str] = None
    default_locale: Optional[str] = None
    plan: Optional[dict] = None         # full plan dict (overrides the above)


@app.get("/api/sessions/{session_id}/data/preview")
async def data_preview(session_id: str, rows: int = 10, page: int = 0,
                       full: bool = False, row: int | None = None) -> dict:
    return _masking_service(session_id).preview_data(
        rows=rows, page=page, full=full, row=row)


@app.get("/api/sessions/{session_id}/data/columns")
async def data_columns(session_id: str) -> dict:
    return {"columns": _masking_service(session_id).columns()}


@app.patch("/api/sessions/{session_id}/data/columns")
async def data_columns_patch(session_id: str, body: ColumnToggleBody) -> dict:
    return _masking_service(session_id).set_column_include(body.column, body.include_in_scan)


@app.get("/api/sessions/{session_id}/mask/plan")
async def mask_get_plan(session_id: str, regenerate: bool = False) -> dict:
    svc = _masking_service(session_id)
    plan = svc.regenerate_plan() if regenerate else svc.get_plan()
    return plan.to_dict()


@app.put("/api/sessions/{session_id}/mask/plan")
async def mask_put_plan(session_id: str, body: MaskPlanBody) -> dict:
    from redibis.masking.plan import MaskingPlan
    svc = _masking_service(session_id)
    if body.plan is not None:
        plan = MaskingPlan.from_dict(body.plan)
    else:
        plan = svc.get_plan()
        if body.columns is not None:
            from redibis.masking.plan import ColumnMaskRule
            plan.columns = [ColumnMaskRule.from_dict(c) for c in body.columns]
        if body.seed is not None:
            plan.seed = body.seed
        if body.default_locale is not None:
            plan.default_locale = body.default_locale
    return svc.save_plan(plan).to_dict()


@app.post("/api/sessions/{session_id}/mask/preview")
async def mask_preview(session_id: str, row: int = 0) -> dict:
    try:
        return _masking_service(session_id).preview_mask(row=row)
    except ValueError as e:
        # e.g. fake/regex rule with no pattern — surface the rule error, not a 500
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/sessions/{session_id}/mask/risk")
async def mask_risk(session_id: str) -> dict:
    return {"risk": _masking_service(session_id).risk()}


@app.post("/api/sessions/{session_id}/mask/apply")
async def mask_apply(session_id: str, format: str = "csv") -> dict:
    try:
        return _masking_service(session_id).apply_export(fmt=format)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/sessions/{session_id}/mask/export")
async def mask_export(session_id: str, format: str = "csv"):
    svc = _masking_service(session_id)
    path = svc.export_path(fmt=format)
    if path is None:
        # No export yet — generate one now.
        result = svc.apply_export(fmt=format)
        path = Path(result["export_path"])
    return FileResponse(str(path), filename=Path(path).name,
                        media_type="application/octet-stream")


@app.get("/api/sessions/{session_id}/mask/manifest")
async def mask_manifest(session_id: str) -> dict:
    manifest = _masking_service(session_id).manifest()
    if manifest is None:
        raise HTTPException(status_code=404, detail="No masked export yet")
    return manifest


@app.get("/api/sessions/{session_id}/mask/audit")
async def mask_audit(session_id: str, run_id: str | None = None) -> dict:
    audit = _masking_service(session_id).audit(run_id=run_id)
    if audit is None:
        raise HTTPException(status_code=404, detail="No audit report for this export")
    return audit


@app.get("/api/sessions/{session_id}/mask/audit/download")
async def mask_audit_download(session_id: str, run_id: str | None = None):
    path = _masking_service(session_id).audit_download_path(run_id=run_id)
    if path is None:
        raise HTTPException(status_code=404, detail="No audit report for this export")
    return FileResponse(str(path), filename=path.name, media_type="application/json")


@app.get("/api/sessions/{session_id}/mask/exports")
async def mask_exports(session_id: str) -> dict:
    return {"exports": _masking_service(session_id).list_exports()}


@app.get("/api/masking/capabilities")
async def masking_capabilities() -> dict:
    from redibis.masking import transforms as _T
    return _T.capabilities()


@app.get("/api/masking/regex-patterns")
async def masking_regex_patterns() -> dict:
    from redibis.masking.regex_library import list_patterns
    return {"patterns": list_patterns()}


class RegexTestBody(BaseModel):
    pattern: Optional[str] = None
    library: Optional[str] = None
    n: int = 3
    deterministic: bool = False
    seed: Optional[str] = None


@app.post("/api/masking/regex-test")
async def masking_regex_test(body: RegexTestBody) -> dict:
    from redibis.masking.regex_library import resolve_pattern
    from redibis.masking.engine import RunKeys
    from redibis.masking import transforms as _T

    pattern = body.pattern or ""
    if not pattern and body.library:
        pattern = resolve_pattern(body.library) or ""
    if not pattern:
        raise HTTPException(status_code=400, detail="pattern or library required")
    keys = RunKeys.mint(seed=body.seed or "regex-test")
    mk = keys.master_key
    samples = []
    for i in range(max(1, min(body.n, 20))):
        rng = _T._value_rng(mk, "k1", "sample", f"{i}", body.deterministic)
        samples.append(_T.fake_from_regex(rng, pattern, deterministic=body.deterministic))
    return {"pattern": pattern, "samples": samples, "deterministic": body.deterministic}


# ═══════════════════════════════════════════════════════════════════════════
# Sub-contracts — staged PII/Quality partials, edited then merged manually
# ═══════════════════════════════════════════════════════════════════════════

class SubContractBody(BaseModel):
    kind: str = "manual"            # "pii" | "quality" | "business" | "manual"
    content: dict[str, Any] = {}
    table: Optional[str] = None
    source: str = "manual"


class SubContractUpdate(BaseModel):
    content: dict[str, Any]


class SubContractMerge(BaseModel):
    ids: Optional[list[str]] = None  # None / empty = merge all staged
    validate_contract: Optional[bool] = None


@app.get("/api/sessions/{session_id}/subcontracts")
async def list_sub_contracts(session_id: str) -> list:
    return [s.summary() for s in _require_session(session_id).sub_contracts]


@app.post("/api/sessions/{session_id}/subcontracts")
async def add_sub_contract(session_id: str, body: SubContractBody) -> dict:
    session = _require_session(session_id)
    sc = session.add_sub_contract(kind=body.kind, content=body.content,
                                  table=body.table, source=body.source)
    return sc.to_dict()


@app.post("/api/sessions/{session_id}/subcontracts/merge")
async def merge_sub_contracts(session_id: str, body: SubContractMerge) -> dict:
    from redibis.services.session_service import merge_session_sub_contracts
    session = _require_session(session_id)
    try:
        merged = merge_session_sub_contracts(
            session, get_contract_store(), ids=body.ids, validate=body.validate_contract)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Merge failed: {e}")
    return {"status": "merged", "count": len(merged), "results": merged}


@app.get("/api/sessions/{session_id}/subcontracts/{sub_id}")
async def get_sub_contract(session_id: str, sub_id: str) -> dict:
    sc = _require_session(session_id).get_sub_contract(sub_id)
    if sc is None:
        raise HTTPException(status_code=404, detail=f"Sub-contract '{sub_id}' not found")
    return sc.to_dict()


@app.put("/api/sessions/{session_id}/subcontracts/{sub_id}")
async def update_sub_contract(session_id: str, sub_id: str, body: SubContractUpdate) -> dict:
    sc = _require_session(session_id).update_sub_contract(sub_id, body.content)
    if sc is None:
        raise HTTPException(status_code=404, detail=f"Sub-contract '{sub_id}' not found")
    return sc.to_dict()


@app.delete("/api/sessions/{session_id}/subcontracts/{sub_id}")
async def delete_sub_contract(session_id: str, sub_id: str) -> dict:
    removed = _require_session(session_id).remove_sub_contract(sub_id)
    if not removed:
        raise HTTPException(status_code=404, detail=f"Sub-contract '{sub_id}' not found")
    return {"status": "deleted", "sub_id": sub_id}


# ═══════════════════════════════════════════════════════════════════════════
# Approved basket — cherry-picked PII columns + quality rules
# ═══════════════════════════════════════════════════════════════════════════

class ApprovePiiBody(BaseModel):
    column: str
    detection: dict[str, Any]
    source_run_id: Optional[str] = None
    source: str = "scan"
    note: str = ""


class ApproveQualityBody(BaseModel):
    column: Optional[str] = None
    rule: dict[str, Any]
    source_run_id: Optional[str] = None
    source: str = "scan"
    note: str = ""


class ApprovedUpdateBody(BaseModel):
    payload: Optional[dict[str, Any]] = None
    column: Optional[str] = None
    label: Optional[str] = None
    note: Optional[str] = None


class ApprovedMergeBody(BaseModel):
    validate_contract: bool = True


@app.get("/api/sessions/{session_id}/approved")
async def list_basket(session_id: str) -> dict:
    return _require_session(session_id).approved.to_dict()


@app.post("/api/sessions/{session_id}/approved/pii")
async def approve_pii(session_id: str, body: ApprovePiiBody) -> dict:
    session = _require_session(session_id)
    from redibis.services.session_service import pii_row_to_fragment
    frag = pii_row_to_fragment(body.detection)
    p = session.approve_property(
        kind="pii", column=body.column, payload=frag,
        source_run_id=body.source_run_id, source=body.source,
        label=body.detection.get("entity_type", ""), note=body.note)
    session.persist_to_disk()
    return p.to_dict()


@app.post("/api/sessions/{session_id}/approved/quality")
async def approve_quality(session_id: str, body: ApproveQualityBody) -> dict:
    session = _require_session(session_id)
    from redibis.services.session_service import quality_row_to_fragment
    frag = quality_row_to_fragment(body.rule)
    label = (body.rule.get("expectation_type") or body.rule.get("rule")
             or frag.get("rule") or frag.get("type") or "")
    col = body.column
    if col in (None, "", "Table-Level"):
        col = "__table__"
    p = session.approve_property(
        kind="quality", column=col, payload=frag,
        source_run_id=body.source_run_id, source=body.source,
        label=label, note=body.note)
    session.persist_to_disk()
    return p.to_dict()


@app.get("/api/sessions/{session_id}/approved/preview")
async def preview_basket(session_id: str) -> dict:
    session = _require_session(session_id)
    from redibis.services.session_service import build_approved_partials
    return {"partials": build_approved_partials(session),
            "summary": session.approved.summary()}


@app.post("/api/sessions/{session_id}/approved/merge")
async def merge_approved_route(session_id: str, body: ApprovedMergeBody) -> dict:
    session = _require_session(session_id)
    from redibis.services.session_service import merge_approved
    try:
        result = merge_approved(session, get_contract_store(), validate=body.validate_contract)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Merge failed: {e}")
    session.persist_to_disk()
    return {"status": "merged", **result}


@app.delete("/api/sessions/{session_id}/approved")
async def clear_approved(session_id: str, only_merged: bool = False) -> dict:
    session = _require_session(session_id)
    n = session.approved.clear(only_merged=only_merged)
    return {"status": "cleared", "removed": n, "summary": session.approved.summary()}


@app.get("/api/sessions/{session_id}/approved/{prop_id}")
async def get_basket_item(session_id: str, prop_id: str) -> dict:
    session = _require_session(session_id)
    p = session.approved.get(prop_id)
    if p is None:
        raise HTTPException(status_code=404, detail=f"No approved property {prop_id!r}")
    return p.to_dict()


@app.put("/api/sessions/{session_id}/approved/{prop_id}")
async def update_approved(session_id: str, prop_id: str, body: ApprovedUpdateBody) -> dict:
    session = _require_session(session_id)
    p = session.approved.update(prop_id, payload=body.payload, note=body.note,
                                column=body.column, label=body.label)
    if p is None:
        raise HTTPException(status_code=404, detail=f"No approved property {prop_id!r}")
    return p.to_dict()


@app.delete("/api/sessions/{session_id}/approved/{prop_id}")
async def delete_approved(session_id: str, prop_id: str) -> dict:
    session = _require_session(session_id)
    ok = session.approved.remove(prop_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"No approved property {prop_id!r}")
    return {"status": "deleted", "prop_id": prop_id, "summary": session.approved.summary()}


# ═══════════════════════════════════════════════════════════════════════════
# Discovery — scoped, single-type runs (PII or quality) over real data
# ═══════════════════════════════════════════════════════════════════════════

class DiscoveryRunBody(BaseModel):
    columns: Optional[list[str]] = None     # None / [] = all columns
    subset_rows: Optional[int] = None       # None = full data
    rules: Optional[list[dict]] = None       # quality only; None = config rule set
    background: bool = False
    # ── Ad-hoc per-run overrides (decoupled from the global config) ──────────
    engines: Optional[str] = None            # pii: "regex" | "gliner" | "both" | "llm"
    gliner_model: Optional[str] = None       # pii: swap the NER model for this run
    regex_patterns: Optional[dict[str, Any]] = None   # pii: ad-hoc regex set
    regex_replace_all: Optional[bool] = None
    regex_confidence: Optional[float] = None
    gliner_confidence: Optional[float] = None
    llm_confidence: Optional[float] = None
    equation_mode: Optional[str] = None

    def to_overrides(self) -> dict:
        return {
            "pii_engines": self.engines,
            "pii_gliner_model": self.gliner_model,
            "pii_regex_confidence": self.regex_confidence,
            "pii_gliner_confidence": self.gliner_confidence,
            "pii_llm_confidence": self.llm_confidence,
            "equation_mode": self.equation_mode,
            "regex_patterns": self.regex_patterns,
            "regex_replace_all": self.regex_replace_all,
        }


def _start_discovery(session, kind, body: "DiscoveryRunBody", background: BackgroundTasks):
    from redibis.services.session_service import execute_discovery_run
    ov = body.to_overrides()
    if body.background:
        _start_scan_job(
            session,
            execute_discovery_run,
            session,
            kind,
            body.columns,
            body.subset_rows,
            body.rules,
            ov,
        )
        return {"status": "started", "kind": kind, "session_id": session.session_id}
    drun = execute_discovery_run(session, kind, columns=body.columns,
                                 subset_rows=body.subset_rows, rules=body.rules,
                                 overrides=ov)
    return drun.to_dict()


@app.post("/api/sessions/{session_id}/discovery/pii")
async def discover_pii(session_id: str, body: DiscoveryRunBody, background: BackgroundTasks) -> dict:
    return _start_discovery(_require_session(session_id), "pii", body, background)


class PiiRegexTestBody(BaseModel):
    pattern: str
    entity_type: str = "CUSTOM"
    test_values: list[str] = []
    column: Optional[str] = None
    sample_count: int = 8
    recognizer_group: str = "structured"  # structured (^$) | free_text (\b)
    normalizer: Optional[str] = None  # e.g. normalize_msisdn_egypt (strip dashes/spaces)


@app.post("/api/sessions/{session_id}/discovery/pii/test-regex")
async def test_pii_regex(session_id: str, body: PiiRegexTestBody) -> dict:
    """Quick regex smoke-test against pasted values and/or a column sample."""
    import re
    from redibis.services.session_service import _load_dataframe

    session = _require_session(session_id)
    pattern = (body.pattern or "").strip()
    if not pattern:
        raise HTTPException(status_code=400, detail="pattern is required")

    df = _load_dataframe(session)

    def _column_values(col: str, limit: int) -> list[str]:
        if col not in df.columns:
            return []
        series = df[col]
        texts = series.dropna().astype(str).map(lambda x: x.strip())
        texts = texts[texts != ""]
        if texts.empty:
            texts = series.astype(str).map(lambda x: x.strip())
            texts = texts[(texts != "") & (texts.str.lower() != "nan")]
        return texts.head(max(1, min(limit, 50))).tolist()

    values: list[str] = [v.strip() for v in (body.test_values or []) if v and str(v).strip()]
    sampled_col: Optional[str] = body.column or None
    if sampled_col:
        for v in _column_values(sampled_col, body.sample_count):
            if v not in values:
                values.append(v)
    if not values:
        for col in df.columns:
            col_vals = _column_values(str(col), body.sample_count)
            if col_vals:
                sampled_col = str(col)
                values = col_vals
                break
    if not values:
        raise HTTPException(
            status_code=400,
            detail="No non-empty values found — upload data with sample rows, paste test values, or pick a column",
        )

    anchored = body.recognizer_group != "free_text"
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        return {"valid": False, "error": str(exc), "hits": [], "match_count": 0}

    from redibis.pii.regex_catalog import regex_test_candidates

    def _debug_code() -> str:
        method = "match" if anchored else "search"
        lines = [
            "import re",
            "",
            f"pattern = {pattern!r}",
            f"compiled = re.compile(pattern)",
            f"method = {method!r}  # structured → match (^$), free_text → search (\\b)",
        ]
        if body.normalizer:
            lines.append(f"normalizer = {body.normalizer!r}")
            lines.append("from redibis.pii.regex_catalog import regex_test_candidates")
        lines.append("")
        for raw in values[:5]:
            text = str(raw)
            lines.append(f'raw = {text!r}')
            if body.normalizer:
                lines.append("for label, candidate in regex_test_candidates(raw, normalizer):")
                lines.append(f"    m = compiled.{method}(candidate)")
                lines.append('    print(label, repr(candidate), "→", "MATCH" if m else "no match")')
            else:
                lines.append(f'm = compiled.{method}(raw)')
                lines.append('print("MATCH" if m else "no match", repr(m.group(0) if m else None))')
            lines.append("")
        if len(values) > 5:
            lines.append(f"# … +{len(values) - 5} more value(s)")
        return "\n".join(lines)

    hits = []
    for raw in values:
        text = str(raw)
        m = None
        tested_as = "raw"
        matched_text = None
        candidates_trace = []
        for label, candidate in regex_test_candidates(text, body.normalizer):
            cm = compiled.match(candidate) if anchored else compiled.search(candidate)
            candidates_trace.append({
                "label": label,
                "value": candidate[:200],
                "match": bool(cm),
            })
            if cm and not m:
                m = cm
                tested_as = label
                matched_text = cm.group(0)[:120]
        hits.append({
            "value": text[:200],
            "match": bool(m),
            "tested_as": tested_as if m else (candidates_trace[0]["label"] if candidates_trace else "raw"),
            "span": [m.start(), m.end()] if m else None,
            "matched_text": matched_text,
            "candidates": candidates_trace,
        })
    return {
        "valid": True,
        "pattern": pattern,
        "entity_type": body.entity_type,
        "recognizer_group": body.recognizer_group,
        "normalizer": body.normalizer,
        "sampled_column": sampled_col,
        "test_values": [v.strip() for v in (body.test_values or []) if v and str(v).strip()],
        "hits": hits,
        "match_count": sum(1 for h in hits if h["match"]),
        "total": len(hits),
        "debug_code": _debug_code(),
    }


@app.post("/api/sessions/{session_id}/discovery/quality")
async def discover_quality(session_id: str, body: DiscoveryRunBody, background: BackgroundTasks) -> dict:
    return _start_discovery(_require_session(session_id), "quality", body, background)


@app.get("/api/sessions/{session_id}/discovery")
async def get_discovery(session_id: str) -> dict:
    session = _require_session(session_id)
    if session.discovery is None:
        return {"discovery_id": None, "runs": [], "run_count": 0}
    return session.discovery.to_dict()


@app.get("/api/sessions/{session_id}/discovery/runs/{run_id}")
async def get_discovery_run(session_id: str, run_id: str) -> dict:
    session = _require_session(session_id)
    drun = session.discovery.get_run(run_id) if session.discovery else None
    if drun is None:
        raise HTTPException(status_code=404, detail=f"Discovery run '{run_id}' not found")
    return drun.to_dict()


# ═══════════════════════════════════════════════════════════════════════════
# Paste GE rule code — SAFE parse (no exec) + evaluate against the sample
# ═══════════════════════════════════════════════════════════════════════════

class ParseRulesBody(BaseModel):
    code: str = ""


class EvaluateRulesBody(BaseModel):
    code: Optional[str] = None              # pasted GE rule text (safely parsed)
    rules: Optional[list[dict]] = None       # or already-parsed rule dicts
    subset_rows: Optional[int] = None        # cap rows for a fast check


@app.post("/api/quality/parse-rules")
async def parse_quality_rules(body: ParseRulesBody) -> dict:
    """Safely parse pasted GE rule code into structured rules. Never executes."""
    from redibis.contracts.rule_code_parser import parse_ge_rules
    out = parse_ge_rules(body.code)
    out["count"] = len(out["rules"])
    return out


@app.post("/api/sessions/{session_id}/quality/evaluate")
async def evaluate_quality_rules(session_id: str, body: EvaluateRulesBody) -> dict:
    """Parse (if `code` given) + evaluate each GE rule against the session sample.

    Only recognized GE expectations run — pasted text is parsed via AST and never
    executed, so arbitrary code cannot run here.
    """
    session = _require_session(session_id)
    from redibis.contracts.rule_code_parser import parse_ge_rules
    from redibis.services.session_service import _load_dataframe

    errors: list[dict] = []
    rules: list[dict] = list(body.rules or [])
    if body.code:
        parsed = parse_ge_rules(body.code)
        errors = parsed["errors"]
        rules = parsed["rules"] + rules
    if not rules:
        return {"results": [], "errors": errors,
                "summary": {"total": 0, "passed": 0, "failed": 0}}

    try:
        df = _load_dataframe(session)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"No data to evaluate against: {e}")

    from redibis.services.discovery_service import DiscoveryService, QualityProbe
    svc = DiscoveryService()
    results: list[dict] = []
    passed = 0
    for r in rules:
        etype = r.get("expectation_type") or r.get("rule")
        col = r.get("column")
        kwargs = dict(r.get("kwargs") or {})
        sql = r.get("sql")
        if etype == "sql" or sql:
            probe = QualityProbe(
                rule="sql", column=col, sql=sql or kwargs.get("sql"),
                kwargs=kwargs, subset_rows=body.subset_rows,
            )
        else:
            probe = QualityProbe(rule=etype, column=col, kwargs=kwargs,
                                 subset_rows=body.subset_rows)
        pr = svc.probe_quality(probe, df)
        ok = bool(pr.success)
        passed += 1 if ok else 0
        results.append({
            "expectation_type": etype if etype != "sql" else "sql",
            "rule": etype if etype != "sql" else "sql",
            "column": col,
            "kwargs": kwargs,
            "sql": sql or kwargs.get("sql"),
            "meta": r.get("meta") or {},
            "success": ok,
            "element_count": pr.element_count,
            "unexpected_count": pr.unexpected_count,
            "unexpected_percent": pr.unexpected_percent,
            "partial_unexpected": pr.partial_unexpected,
            "observed_value": pr.observed_value,
            "reason": (
                ""
                if ok
                else (
                    f"{pr.unexpected_count} violation(s)"
                    if pr.unexpected_count
                    else str(pr.observed_value or "check failed")
                )
            ),
        })
    return {"results": results, "errors": errors,
            "summary": {"total": len(results), "passed": passed,
                        "failed": len(results) - passed}}


class ExportPackageBody(BaseModel):
    dropped_indices: Optional[list[int]] = None
    schedule: str = "0 6 * * *"
    rules: Optional[list[dict]] = None       # structured curated rules
    code: Optional[str] = None                # or full/fragment Python (parsed only)


class FullCodeBody(BaseModel):
    dropped_indices: Optional[list[int]] = None
    rules: Optional[list[dict]] = None       # structured curated rules
    code: Optional[str] = None                # or full/fragment Python (parsed only)
    engine: str = "spark"                     # "spark" (default) | "pandas"


def _resolve_quality_rules(session, table: str, body):
    """Shared curated-rule resolution for the code and package endpoints."""
    from redibis.services.quality_code import resolve_curated_rules

    return resolve_curated_rules(
        session=session,
        table=table,
        pasted_code=getattr(body, "code", None),
        explicit_rules=getattr(body, "rules", None),
        dropped_indices=getattr(body, "dropped_indices", None),
        store=get_contract_store(),
    )


@app.post("/api/sessions/{session_id}/quality/full-code")
async def generate_full_quality_code(session_id: str, body: FullCodeBody):
    """Return one complete, Jupyter-ready Python program for the curated rules.

    Accepts either structured curated rules or pasted Python; pasted text goes
    through the AST-only parser and is never executed. Every rule and parameter
    is embedded as a readable literal, so the returned program loads no
    Redibis-generated rule JSON/YAML at runtime — only the user's dataset.
    """
    from fastapi.responses import Response

    session = _require_session(session_id)
    table = session.table_name
    if not table:
        raise HTTPException(status_code=400, detail="Session has no table configured")

    from redibis.services.quality_code import render_quality_program

    curated = _resolve_quality_rules(session, table, body)
    try:
        code = render_quality_program(table=table, curated=curated, engine=body.engine)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    safe = table.replace(".", "_")
    return Response(
        content=code,
        media_type="text/x-python",
        headers={
            "Content-Disposition": f'attachment; filename="{safe}_quality.py"',
            "X-Redibis-Rule-Source": curated.rule_source,
            "X-Redibis-Rule-Count": str(len(curated.effective_rules)),
        },
    )


@app.post("/api/sessions/{session_id}/quality/export-package")
async def export_quality_monitoring_package(session_id: str, body: ExportPackageBody):
    """Build and return a per-table monitoring package zip (rules + DAG + runner)."""
    from fastapi.responses import Response

    session = _require_session(session_id)
    table = session.table_name
    if not table:
        raise HTTPException(status_code=400, detail="Session has no table configured")

    from redibis.quality.monitoring_package import build_monitoring_zip

    curated = _resolve_quality_rules(session, table, body)
    blob = build_monitoring_zip(
        table=table,
        contract=curated.contract,
        rules=curated.rules,
        dropped_rule_ids=curated.dropped_rule_ids,
        schedule=body.schedule,
        rule_source=curated.rule_source,
    )
    safe = table.replace(".", "_")
    return Response(
        content=blob,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{safe}-monitoring.zip"'},
    )


# ═══════════════════════════════════════════════════════════════════════════
# Editable global config — the edit-then-rerun loop
# ═══════════════════════════════════════════════════════════════════════════

class GlobalConfigPatch(BaseModel):
    """Partial update of the session's global config scalar fields."""
    fields: dict[str, Any]


class RegexPatternBody(BaseModel):
    key: str
    entry: dict[str, Any]


class RegexSetBody(BaseModel):
    name: Optional[str] = None
    replace_all: bool = False
    patterns: dict[str, Any] = {}


class QualityRuleBody(BaseModel):
    rule: str
    column: Optional[str] = None
    kwargs: dict[str, Any] = {}


class QualityRuleSetBody(BaseModel):
    name: Optional[str] = None
    rules: list[dict] = []


@app.get("/api/sessions/{session_id}/config")
async def get_global_config(session_id: str) -> dict:
    return _require_session(session_id).common_config.to_dict()


@app.patch("/api/sessions/{session_id}/config")
async def patch_global_config(session_id: str, body: GlobalConfigPatch) -> dict:
    session = _require_session(session_id)
    cc = session.common_config
    allowed = {f for f in cc.__dataclass_fields__ if f not in ("regex_set", "quality_rule_set")}
    for k, v in body.fields.items():
        if k in allowed:
            setattr(cc, k, v)
    return cc.to_dict()


# ── Regex list editing (PII discovery list) ─────────────────────────────────

@app.put("/api/sessions/{session_id}/config/regex")
async def set_regex_set(session_id: str, body: RegexSetBody) -> dict:
    from redibis.pii.regex_overrides import RegexSet
    session = _require_session(session_id)
    session.common_config.regex_set = RegexSet(
        name=body.name, replace_all=body.replace_all, patterns=dict(body.patterns))
    return session.common_config.regex_set.to_dict()


@app.post("/api/sessions/{session_id}/config/regex/patterns")
async def add_regex_pattern(session_id: str, body: RegexPatternBody) -> dict:
    session = _require_session(session_id)
    session.common_config.regex_set.add_pattern(body.key, body.entry)
    return session.common_config.regex_set.to_dict()


@app.delete("/api/sessions/{session_id}/config/regex/patterns/{key}")
async def remove_regex_pattern(session_id: str, key: str) -> dict:
    session = _require_session(session_id)
    removed = session.common_config.regex_set.remove_pattern(key)
    return {"removed": removed, "regex_set": session.common_config.regex_set.to_dict()}


@app.post("/api/sessions/{session_id}/config/regex/load/{name}")
async def load_regex_into_session(session_id: str, name: str) -> dict:
    from redibis.pii.regex_overrides import RegexSet
    session = _require_session(session_id)
    try:
        session.common_config.regex_set = RegexSet.from_overrides(
            get_config_store().load_regex_config(name), name=name)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Regex config '{name}' not found")
    return session.common_config.regex_set.to_dict()


# ── Quality rules editing (Quality discovery list) ──────────────────────────

@app.put("/api/sessions/{session_id}/config/quality")
async def set_quality_rule_set(session_id: str, body: QualityRuleSetBody) -> dict:
    from redibis.quality.rule_set import QualityRuleSet
    session = _require_session(session_id)
    session.common_config.quality_rule_set = QualityRuleSet(name=body.name, rules=list(body.rules))
    return session.common_config.quality_rule_set.to_dict()


@app.post("/api/sessions/{session_id}/config/quality/rules")
async def add_quality_rule(session_id: str, body: QualityRuleBody) -> dict:
    session = _require_session(session_id)
    session.common_config.quality_rule_set.add_rule(body.rule, body.column, body.kwargs)
    return session.common_config.quality_rule_set.to_dict()


@app.delete("/api/sessions/{session_id}/config/quality/rules/{index}")
async def remove_quality_rule(session_id: str, index: int) -> dict:
    session = _require_session(session_id)
    removed = session.common_config.quality_rule_set.remove_rule(index)
    return {"removed": removed, "quality_rule_set": session.common_config.quality_rule_set.to_dict()}


@app.post("/api/sessions/{session_id}/config/quality/load/{name}")
async def load_quality_into_session(session_id: str, name: str) -> dict:
    from redibis.quality.rule_set import QualityRuleSet
    session = _require_session(session_id)
    try:
        session.common_config.quality_rule_set = QualityRuleSet(
            name=name, rules=get_config_store().load_quality_config(name))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Quality config '{name}' not found")
    return session.common_config.quality_rule_set.to_dict()


# ═══════════════════════════════════════════════════════════════════════════
# Debug + persistence
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/sessions/{session_id}/debug")
async def debug_session(session_id: str) -> dict:
    return _require_session(session_id).to_debug_dict()


@app.post("/api/sessions/{session_id}/flush")
async def flush_session(session_id: str) -> dict:
    import asyncio

    session = _require_session(session_id)
    path = await asyncio.to_thread(session.persist_to_disk)
    return {"status": "flushed", "session_id": session_id, "path": str(path)}


@app.post("/api/sessions/flush_all")
async def flush_all_sessions() -> dict:
    import asyncio

    from redibis.services.session_service import session_manager

    async def _flush_one(session) -> str | None:
        try:
            return str(await asyncio.to_thread(session.persist_to_disk))
        except Exception:
            return None

    sessions = [
        session_manager.get_session(sid)
        for sid in list(session_manager._sessions.keys())
    ]
    sessions = [s for s in sessions if s is not None]
    paths = [p for p in await asyncio.gather(*[_flush_one(s) for s in sessions]) if p]
    return {"status": "flushed", "count": len(paths), "paths": paths}


@app.get("/api/scan_output/{session_id}")
async def view_persisted(session_id: str):
    """Return the on-disk persisted session.json (what's flushed under scan_output)."""
    session_file = _scan_output_dir() / session_id / "session.json"
    if not session_file.exists():
        raise HTTPException(status_code=404, detail="No persisted session on disk")
    return FileResponse(session_file, media_type="application/json")


# ═══════════════════════════════════════════════════════════════════════════
# Named config lists (regex + quality) — settings / discovery registry
# ═══════════════════════════════════════════════════════════════════════════

class RegexConfigBody(BaseModel):
    name: str
    description: str = ""
    overrides: dict[str, Any] = {}


class QualityConfigBody(BaseModel):
    name: str
    description: str = ""
    rules: list[dict] = []
    ge_code: str = ""


@app.get("/api/configs/regex")
async def list_regex_configs() -> list:
    return get_config_store().list_regex_configs()


@app.get("/api/configs/regex/{name}")
async def get_regex_config(name: str) -> dict:
    from redibis.pii.regex_catalog import build_effective_catalog

    try:
        overrides = get_config_store().load_regex_config(name)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Regex config '{name}' not found")
    effective = build_effective_catalog(overrides)
    active_count = sum(1 for e in effective.values() if e.active)
    uses_builtin = not overrides.replace_all and not overrides.add and not overrides.remove
    return {
        "name": name,
        "uses_builtin_catalog": uses_builtin,
        "effective_pattern_count": active_count,
        "config": overrides.to_dict(),
    }


@app.post("/api/configs/regex")
async def save_regex_config(body: RegexConfigBody) -> dict:
    from redibis.pii.regex_overrides import RegexOverrides
    overrides = RegexOverrides.from_dict(body.overrides)
    loc = get_config_store().save_regex_config(body.name, overrides, body.description)
    return {"status": "saved", "name": body.name, "location": str(loc)}


@app.delete("/api/configs/regex/{name}")
async def delete_regex_config(name: str) -> dict:
    return {"deleted": get_config_store().delete_regex_config(name), "name": name}


@app.get("/api/configs/quality")
async def list_quality_configs() -> list:
    return get_config_store().list_quality_configs()


@app.get("/api/configs/quality/{name}")
async def get_quality_config(name: str) -> dict:
    try:
        doc = get_config_store().load_quality_config_doc(name)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Quality config '{name}' not found")
    return {
        "name": name,
        "rules": doc.get("rules", []),
        "ge_code": doc.get("ge_code", ""),
        "description": doc.get("description", ""),
    }


@app.post("/api/configs/quality")
async def save_quality_config(body: QualityConfigBody) -> dict:
    loc = get_config_store().save_quality_config(
        body.name, body.rules, body.description, ge_code=body.ge_code,
    )
    return {"status": "saved", "name": body.name, "location": str(loc)}


@app.delete("/api/configs/quality/{name}")
async def delete_quality_config(name: str) -> dict:
    return {"deleted": get_config_store().delete_quality_config(name), "name": name}


_GLOBAL_SETTINGS_ALLOWED = frozenset({
    "agentic_defaults",
    "automerge",
    "checksum",
    "llm",
    "llm_defaults",
    "pii",
    "revision",
    "schema_version",
    "ui_prefs",
    "updated_at",
})
_SETTINGS_SECRET_MARKERS = (
    "api_key",
    "credential",
    "dsn",
    "password",
    "provenance",
    "secret",
    "token",
)


def _redact_settings(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).lower()
            if any(marker in normalized for marker in _SETTINGS_SECRET_MARKERS):
                out[str(key)] = bool(item)
            else:
                out[str(key)] = _redact_settings(item)
        return out
    if isinstance(value, list):
        return [_redact_settings(item) for item in value]
    return value


def _settings_contains_secret_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if any(marker in str(key).lower() for marker in _SETTINGS_SECRET_MARKERS):
                return True
            if _settings_contains_secret_key(item):
                return True
    elif isinstance(value, list):
        return any(_settings_contains_secret_key(item) for item in value)
    return False


def _public_global_settings(settings: dict[str, Any]) -> dict[str, Any]:
    return _redact_settings({
        key: value for key, value in settings.items()
        if key in _GLOBAL_SETTINGS_ALLOWED
    })


@app.get("/api/settings")
async def get_settings() -> dict:
    return {
        "backend": type(get_backend_store()).__name__,
        "runs_bucket": get_runs_bucket(),
        "contracts_bucket": get_contracts_bucket(),
        "scan_output_dir": str(_scan_output_dir()),
        "configs_dir": str(_configs_dir()),
        "regex_configs": get_config_store().list_regex_configs(),
        "quality_configs": get_config_store().list_quality_configs(),
        "global_settings": _public_global_settings(
            get_config_store().load_global_settings()
        ),
        "timeouts": _ui_timeouts(),
    }


class GlobalSettingsBody(BaseModel):
    """Free-form UI / app preferences blob persisted via the config store."""
    settings: dict[str, Any] = {}


@app.get("/api/settings/global")
async def get_global_settings() -> dict:
    from dataclasses import asdict

    from redibis.config import PIIConfig

    out = get_config_store().load_global_settings()
    out.setdefault("ui_prefs", {})
    if isinstance(out["ui_prefs"], dict):
        out["ui_prefs"].update(_ui_timeouts())
    pii_block = out.setdefault("pii", {})
    if isinstance(pii_block, dict):
        defaults = asdict(PIIConfig())
        for key in (
            "sample_size",
            "use_phonenumbers",
            "default_region",
            "msisdn_valid_rate_min",
            "phone_gate_conf",
            "msisdn_prefixes",
        ):
            pii_block.setdefault(key, defaults[key])
    return _public_global_settings(out)


@app.put("/api/settings/global")
async def put_global_settings(body: GlobalSettingsBody) -> dict:
    from redibis.config import deep_merge

    gs = get_config_store().load_global_settings()
    incoming = dict(body.settings or {})
    unknown = sorted(set(incoming) - _GLOBAL_SETTINGS_ALLOWED)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported global setting keys: {', '.join(unknown)}",
        )
    if _settings_contains_secret_key(incoming):
        raise HTTPException(
            status_code=400,
            detail="secret-bearing settings must use environment or provider credential references",
        )
    agentic = incoming.get("agentic_defaults")
    if isinstance(agentic, dict) and "planner_provider" in agentic and "planner_mode" not in agentic:
        agentic = dict(agentic)
        agentic["planner_mode"] = "llm" if str(agentic.get("planner_provider") or "").strip() else "heuristic"
        incoming["agentic_defaults"] = agentic
    merged = deep_merge(gs, incoming)
    get_config_store().save_global_settings(merged)
    return {"status": "saved", "settings": _public_global_settings(merged)}


@app.get("/api/settings/runtime")
async def get_runtime_settings() -> dict:
    """Effective, redacted runtime configuration for the settings UI."""
    cfg = _redibis_config()
    planner = _resolved_planner_settings()
    config_path = os.environ.get("REDIBIS_CONFIG", "")
    llm = _global_llm_defaults()
    behavior = getattr(cfg, "behavior", None)
    return {
        "config_source": "REDIBIS_CONFIG" if config_path else "built_in_defaults",
        "config_path_configured": bool(config_path),
        "note": "Deployment settings are read-only here and require a process restart.",
        "sections": {
            "scan": {
                "scan_types": ", ".join(cfg.scan_types),
                "source_engine": cfg.source.engine,
                "profiling_engine": cfg.profiling.engine,
                "quality_docs": bool(cfg.quality.generate_ge_docs),
                "editable": False,
                "restart_required": True,
            },
            "pii_ner": {
                "engines": cfg.pii.engines,
                "equation_mode": cfg.pii.equation_mode,
                "sample_size": int(
                    (load_global_settings().get("pii") or {}).get(
                        "sample_size", cfg.pii.sample_size
                    )
                ),
                "ner_type": cfg.pii.ner.type,
                "ner_model_configured": bool(cfg.pii.ner.model_path),
                "models_dir_configured": bool(cfg.pii.models_dir),
                "editable": True,
                "restart_required": False,
            },
            "agents": {
                "enabled": bool(cfg.agents.enabled),
                "single_table_executor": cfg.agents.single_table_executor,
                "batch_executor": cfg.agents.batch_executor,
                "copilotkit_enabled": bool(cfg.agents.copilotkit_enabled),
                "allow_external_codegen": bool(cfg.agents.allow_external_codegen),
                "codegen_service_configured": bool(cfg.agents.codegen_service_url),
                "dynamic_sandbox_enabled": bool(cfg.agents.dynamic_sandbox_enabled),
                "editable": False,
                "restart_required": True,
            },
            "planner": {
                **planner,
                "editable": True,
                "restart_required": False,
            },
            "enrichment": {
                "enabled": bool(llm.get("enabled")),
                "provider": llm.get("provider", ""),
                "model": llm.get("model", ""),
                "source": "global_settings" if llm.get("provider") else "not_configured",
                "endpoint_configured": bool(llm.get("endpoint_url")),
                "include_pii_evidence": bool(cfg.enrich.include_pii_evidence),
                "editable": True,
                "restart_required": False,
            },
            "rai": {
                "enabled": bool(cfg.rai.enabled),
                "mode": cfg.rai.mode,
                "enforce": bool(cfg.rai.enforce),
                "block_external_raw_pii": bool(cfg.rai.block_external_raw_pii),
                "hard_block_external_pii": bool(cfg.rai.hard_block_external_pii),
                "default_residency": cfg.rai.default_residency,
                "allowed_models_count": len(cfg.rai.allowed_models),
                "denied_models_count": len(cfg.rai.denied_models),
                "editable": False,
                "restart_required": True,
            },
            "memory": {
                "enabled": bool(cfg.memory.enabled),
                "store": cfg.memory.store,
                "dsn_configured": bool(cfg.memory.dsn_ref),
                "embedding_provider": cfg.memory.embedding_provider,
                "embedding_model": cfg.memory.embedding_model,
                "top_k": cfg.memory.top_k,
                "async_writes": bool(cfg.memory.async_writes),
                "editable": False,
                "restart_required": True,
            },
            "classification": {
                "enabled": bool(cfg.classification.enabled),
                "policy_pack": cfg.classification.policy_pack,
                "default_jurisdiction": cfg.classification.default_jurisdiction,
                "edge_rules_enabled": bool(cfg.classification.edge_rules_enabled),
                "editable": False,
                "restart_required": True,
            },
            "behavior": {
                "enabled": bool(getattr(behavior, "enabled", False)),
                "mode": str(getattr(behavior, "mode", "off")),
                "editable": False,
                "restart_required": True,
            },
            "observability": {
                "log_level": cfg.observability.log_level,
                "persist_run_log": bool(cfg.observability.persist_run_log),
                "decision_log": bool(cfg.observability.decision_log),
                "llm_debug": bool(cfg.observability.llm_debug),
                "otel_enabled": bool(cfg.observability.otel.enabled),
                "otel_exporter": cfg.observability.otel.exporter,
                "otlp_endpoint_configured": bool(cfg.observability.otel.otlp_endpoint),
                "editable": False,
                "restart_required": True,
            },
            "storage": {
                "backend": cfg.storage.backend,
                "runs_bucket": cfg.storage.runs_bucket,
                "contracts_bucket": cfg.storage.contracts_bucket,
                "editable": False,
                "restart_required": True,
            },
        },
    }


# ═══════════════════════════════════════════════════════════════════════════
# Regex catalog (default patterns, read-only)
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/regex-catalog")
async def get_regex_catalog(
    group: Optional[str] = None,
    entity_type: Optional[str] = None,
    script: Optional[str] = None,
    active_only: bool = True,
) -> dict:
    from redibis.pii.regex_catalog import list_catalog
    patterns = list_catalog(group=group, active_only=active_only,
                            entity_type=entity_type, script=script)
    return {"count": len(patterns), "patterns": patterns}


@app.get("/api/regex-catalog/{name}")
async def get_regex_catalog_pattern(name: str) -> dict:
    from redibis.pii.regex_catalog import get_pattern
    entry = get_pattern(name)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Pattern '{name}' not found in catalog")
    return entry


# ═══════════════════════════════════════════════════════════════════════════
# PII export + tuning API (catalog, NER inventory, overrides, labels)
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/pii/regex")
async def export_pii_regex_catalog(
    format: str = Query("json", alias="format"),
    active_only: bool = False,
) -> Response:
    from redibis.pii.export import export_regex_catalog
    from redibis.pii.regex_overrides import RegexOverrides

    gs = get_config_store().load_global_settings()
    overrides_raw = gs.get("pii_regex_overrides")
    overrides = RegexOverrides.from_dict(overrides_raw) if overrides_raw else None
    fmt = (format or "json").lower()
    if fmt not in ("json", "yaml", "csv"):
        raise HTTPException(status_code=400, detail="format must be json, yaml, or csv")
    body = export_regex_catalog(overrides=overrides, active_only=active_only, fmt=fmt)
    media = {
        "json": "application/json",
        "yaml": "application/x-yaml",
        "csv": "text/csv",
    }[fmt]
    return Response(content=body, media_type=media)


@app.get("/api/pii/regex/{name}")
async def get_pii_regex_pattern(name: str) -> dict:
    from redibis.pii.regex_catalog import get_pattern, build_effective_catalog
    from redibis.pii.regex_overrides import RegexOverrides

    gs = get_config_store().load_global_settings()
    overrides_raw = gs.get("pii_regex_overrides")
    overrides = RegexOverrides.from_dict(overrides_raw) if overrides_raw else None
    cat = build_effective_catalog(overrides)
    entry = cat.get(name)
    if entry is None:
        entry_dict = get_pattern(name)
        if entry_dict is None:
            raise HTTPException(status_code=404, detail=f"Pattern '{name}' not found")
        return entry_dict
    from redibis.pii.regex_catalog import _entry_to_dict
    return _entry_to_dict(name, entry)


class PiiRegexOverridesBody(BaseModel):
    add: dict[str, Any] = {}
    remove: list[str] = []
    replace_all: bool = False


@app.post("/api/pii/regex/overrides")
async def save_pii_regex_overrides(body: PiiRegexOverridesBody) -> dict:
    from redibis.pii.regex_overrides import RegexOverrides

    overrides = RegexOverrides(
        add=body.add,
        remove=body.remove,
        replace_all=body.replace_all,
    )
    gs = get_config_store().load_global_settings()
    gs["pii_regex_overrides"] = overrides.to_dict()
    loc = get_config_store().save_global_settings(gs)
    return {"status": "saved", "location": str(loc), "overrides": overrides.to_dict()}


@app.get("/api/pii/ner/models")
async def list_pii_ner_models(models_dir: Optional[str] = Query(None)) -> dict:
    from redibis.pii.export import export_ner_models

    specs = export_ner_models(models_dir)
    return {"count": len(specs), "models": specs}


@app.get("/api/pii/ner/labels")
async def get_pii_ner_labels(
    session_id: Optional[str] = Query(None),
    model_path: Optional[str] = Query(None),
    models_dir: Optional[str] = Query(None),
) -> dict:
    from redibis.pii.ner_registry import NERModelRegistry
    from redibis.services.session.config import _effective_ner_model_path

    gs = get_config_store().load_global_settings()
    stored = gs.get("pii_ner_labels")
    resolved_path = (model_path or "").strip()
    resolved_dir = (models_dir or "").strip()
    if session_id:
        try:
            session = _require_session(session_id)
            cc = session.common_config
            if not resolved_path:
                resolved_path = _effective_ner_model_path(cc)
            if not resolved_dir:
                resolved_dir = (cc.pii_models_dir or "").strip()
            if isinstance(cc.pii_ner_labels, list) and cc.pii_ner_labels:
                stored = cc.pii_ner_labels
        except HTTPException:
            pass
    if not resolved_path:
        from redibis.services.session.config import GlobalConfig

        probe = GlobalConfig(pii_models_dir=resolved_dir)
        resolved_path = _effective_ner_model_path(probe)
    labels, source = NERModelRegistry.resolve_effective_labels(
        stored=stored if isinstance(stored, list) else None,
        model_path=resolved_path or None,
    )
    return {"labels": labels, "source": source}


class PiiNerLabelsBody(BaseModel):
    labels: list[str] = []


@app.put("/api/pii/ner/labels")
async def put_pii_ner_labels(body: PiiNerLabelsBody) -> dict:
    labels = [x.strip() for x in (body.labels or []) if x and str(x).strip()]
    gs = get_config_store().load_global_settings()
    gs["pii_ner_labels"] = labels
    loc = get_config_store().save_global_settings(gs)
    return {"status": "saved", "location": str(loc), "labels": labels, "source": "config"}


def _seed_pii_ner_labels_from_global(cc) -> None:
    """Copy persisted NER labels into a new session config when unset."""
    if cc.pii_ner_labels:
        return
    gs = get_config_store().load_global_settings()
    stored = gs.get("pii_ner_labels")
    if isinstance(stored, list) and stored:
        cc.pii_ner_labels = list(stored)


# ═══════════════════════════════════════════════════════════════════════════
# Config export / import (regex + quality bundle)
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/configs/export")
async def export_configs(include_catalog: bool = Query(True)) -> dict:
    """Export saved regex + quality configs; optionally include built-in catalog snapshot."""
    from redibis.pii.export import list_regex_catalog_summary
    from redibis.pii.regex_catalog import build_effective_catalog

    regex = {}
    for meta in get_config_store().list_regex_configs():
        name = meta["name"]
        try:
            overrides = get_config_store().load_regex_config(name)
            uses_builtin = bool(meta.get("uses_builtin_catalog"))
            effective = build_effective_catalog(overrides)
            active_count = sum(1 for e in effective.values() if e.active)
            entry: dict[str, Any] = {
                "description": meta.get("description", ""),
                "uses_builtin_catalog": uses_builtin,
                "effective_pattern_count": active_count,
                "config": overrides.to_dict(),
            }
            if uses_builtin and include_catalog:
                entry["effective_patterns"] = list_regex_catalog_summary(
                    overrides=overrides, active_only=True,
                )
            regex[name] = entry
        except Exception:
            continue
    quality = {}
    for meta in get_config_store().list_quality_configs():
        name = meta["name"]
        try:
            quality[name] = {
                "description": meta.get("description", ""),
                "rules": get_config_store().load_quality_config(name),
            }
        except Exception:
            continue
    out: dict[str, Any] = {
        "version": 1,
        "regex": regex,
        "quality": quality,
        "defaults": {
            "regex_config": "redibis-default",
            "quality_config": "merchant-curated-demo",
        },
    }
    if include_catalog:
        out["builtin_regex_catalog"] = list_regex_catalog_summary(active_only=True)
    return out


@app.post("/api/configs/seed-defaults")
async def seed_default_configs() -> dict:
    """Install packaged redibis-default regex + demo quality configs when missing."""
    from redibis.store.config_bootstrap import ensure_bundled_configs

    seeded = ensure_bundled_configs(get_config_store())
    return {"status": "ok", "seeded": seeded}


class ConfigBundle(BaseModel):
    version: int = 1
    regex: dict[str, Any] = {}
    quality: dict[str, Any] = {}
    overwrite: bool = True


@app.post("/api/configs/import")
async def import_configs(bundle: ConfigBundle) -> dict:
    """Import a previously exported bundle of regex + quality configs."""
    from redibis.pii.regex_overrides import RegexOverrides
    existing_regex = {m["name"] for m in get_config_store().list_regex_configs()}
    existing_quality = {m["name"] for m in get_config_store().list_quality_configs()}
    saved = {"regex": [], "quality": [], "skipped": []}
    for name, payload in (bundle.regex or {}).items():
        if name in existing_regex and not bundle.overwrite:
            saved["skipped"].append(f"regex:{name}")
            continue
        overrides = RegexOverrides.from_dict(payload.get("config", payload))
        get_config_store().save_regex_config(name, overrides, payload.get("description", ""))
        saved["regex"].append(name)
    for name, payload in (bundle.quality or {}).items():
        if name in existing_quality and not bundle.overwrite:
            saved["skipped"].append(f"quality:{name}")
            continue
        rules = payload.get("rules", payload if isinstance(payload, list) else [])
        get_config_store().save_quality_config(name, rules, payload.get("description", "") if isinstance(payload, dict) else "")
        saved["quality"].append(name)
    return {"status": "imported", **saved}


# ═══════════════════════════════════════════════════════════════════════════
# Contracts (read-only) — list / detail / history / audit / html
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/contracts")
async def list_contracts() -> list:
    """List every table that has an active contract, with a small summary."""
    out = []
    for table in store_op(get_contract_store().list_tables):
        active = store_op(get_contract_store().get_active, table) or {}
        history = store_op(get_contract_store().get_history, table, limit=1)
        meta = store_op(get_contract_store().get_metadata, table)
        out.append({
            "table": table,
            "version": active.get("version"),
            "contract_uuid": active.get("contract_uuid"),
            "workflows": sorted({p.get("workflow") for p in meta.get("provenance", [])
                                 if p.get("workflow")}),
            "last_updated": (meta.get("telemetry") or {}).get("last_updated")
                            or (history[0].timestamp if history else None),
        })
    return out


@app.get("/api/contracts/{table}")
async def get_contract(table: str) -> dict:
    """Slim contract spec only (no operational telemetry)."""
    active = store_op(get_contract_store().get_active, table)
    if active is None:
        raise HTTPException(status_code=404, detail=f"No active contract for '{table}'")
    return active


@app.get("/api/contracts/{table}/metadata")
async def get_contract_metadata(table: str) -> dict:
    """Operational telemetry: provenance, pii_summary, last_updated, pii_decisions."""
    if store_op(get_contract_store().get_active, table) is None:
        raise HTTPException(status_code=404, detail=f"No active contract for '{table}'")
    return store_op(get_contract_store().get_metadata, table)


@app.get("/api/contracts/{table}/export-package")
async def export_contract_package(table: str) -> dict:
    """Full JSON bundle for catalog integration (OpenMetadata, Elasticsearch, etc.)."""
    try:
        return store_op(get_contract_store().export_integration_package, table)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


def _catalog_service():
    from redibis.config import RedibisConfig
    from redibis.services.catalog_service import CatalogService

    cfg_path = os.environ.get("REDIBIS_CONFIG")
    cfg = RedibisConfig.from_yaml(cfg_path) if cfg_path else RedibisConfig.default()
    return CatalogService.from_redibis_config(get_contract_store(), cfg)


@app.get("/api/catalog/backends")
async def list_catalog_backends() -> dict:
    svc = _catalog_service()
    return {"backends": svc.list_backends(), "default": svc.config.backend}


@app.post("/api/catalog/push/{table}")
async def push_contract_to_catalog(
    table: str,
    dry_run: bool = Query(False),
    backend: Optional[str] = Query(None),
) -> dict:
    svc = _catalog_service()
    try:
        result = svc.push(table, dry_run=dry_run, backend=backend)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {
        "table": result.table,
        "backend": result.backend,
        "entity_fqn": result.entity_fqn,
        "entity_id": result.entity_id,
        "contract_id": result.contract_id,
        "glossary_count": result.glossary_count,
        "dry_run": result.dry_run,
        "preview": result.preview,
    }


@app.get("/api/catalog/status/{table}")
async def catalog_push_status(table: str, backend: Optional[str] = Query(None)) -> dict:
    svc = _catalog_service()
    try:
        st = svc.status(table, backend=backend)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {
        "table": st.table,
        "backend": st.backend,
        "contract_version": st.contract_version,
        "last_push": st.last_push,
        "in_sync": st.in_sync,
    }


@app.get("/api/contracts/{table}/history")
async def get_contract_history(table: str, limit: int = 50) -> list:
    return [
        {
            "run_uuid": e.run_uuid, "run_id": e.run_id, "workflow": e.workflow,
            "timestamp": e.timestamp, "version": e.version,
            "contributed_fields": e.contributed_fields,
        }
        for e in get_contract_store().get_history(table, limit=limit)
    ]


@app.get("/api/contracts/{table}/audit/{run_uuid}")
async def get_contract_audit(table: str, run_uuid: str) -> dict:
    snap = get_contract_store().get_audit_snapshot(table, run_uuid)
    if snap is None:
        raise HTTPException(status_code=404, detail="Audit snapshot not found")
    return snap


# ── PII decisions (strip / add PII on a contract column) ─────────────────────
# A column is PII when it carries a `pii` block / maskingPolicy / classification
# / pii tags. The merge can only ADD those (tags union, omitted fields kept), so
# removal goes through the decision overlay enforced inside ContractStore.upsert.

class StripPiiBody(BaseModel):
    decided_by: str = "web"
    run_id: str = ""


class AddPiiColumnBody(BaseModel):
    entity_type: str
    confidence: float = 1.0
    arabic_aware: bool = False
    decided_by: str = "web"
    run_id: str = ""


class SamplingConsentBody(BaseModel):
    approved: bool = False
    approved_by: str = "web"


def _column_is_pii(prop: dict) -> bool:
    from redibis.contracts.privacy import column_is_pii
    return column_is_pii(prop)


@app.get("/api/contracts/{table}/sampling-consent")
async def get_sampling_consent(table: str) -> dict:
    """Return steward sampling-consent flags for memory redaction (optional)."""
    return {"table": table, "columns": get_contract_store().get_sampling_consent(table)}


@app.post("/api/contracts/{table}/columns/{column}/sampling-consent")
async def set_sampling_consent(table: str, column: str, body: SamplingConsentBody) -> dict:
    """Approve or revoke consent to persist masked/hashed samples in column memory."""
    entry = get_contract_store().set_sampling_consent(
        table,
        column,
        approved=body.approved,
        approved_by=body.approved_by,
    )
    return {"table": table, "column": column, "consent": entry}


def _memory_hints_for_contract(table: str, active: dict) -> list[dict]:
    from redibis.memory.retriever import get_context_retriever, hint_to_dict
    from redibis.memory.writer import fingerprint_from_column_prop

    mc = get_contract_store().memory_config
    retriever = get_context_retriever(
        mc,
        memory_store=getattr(get_contract_store(), "_memory_store", None),
    )
    if retriever is None:
        return []
    columns: list[dict] = []
    for schema_obj in active.get("schema", []) or []:
        for prop in schema_obj.get("properties", []) or []:
            if not isinstance(prop, dict):
                continue
            col = prop.get("name")
            if not col:
                continue
            fp = fingerprint_from_column_prop(
                table, str(col), prop, domain=mc.domain,
            )
            hints = retriever.retrieve(fp)
            if not hints:
                continue
            columns.append({
                "column": col,
                "hints": [hint_to_dict(h) for h in hints],
            })
    return columns


@app.get("/api/memory/status")
async def memory_status() -> dict:
    """Read-only memory feature flag + store health (additive; off when memory disabled)."""
    mc = get_contract_store().memory_config
    out = {
        "enabled": mc.enabled,
        "store": mc.store,
        "domain": mc.domain,
        "embedding_provider": mc.embedding_provider,
        "top_k": mc.top_k,
        "min_similarity": mc.min_similarity,
    }
    if mc.enabled:
        out["store_ready"] = getattr(get_contract_store(), "_memory_store", None) is not None
        from redibis.memory.async_writer import memory_write_stats

        out["async_writes"] = mc.async_writes
        out["write_stats"] = memory_write_stats().to_dict()
    return out


@app.get("/api/vector/status")
async def vector_status() -> dict:
    """Golden vector store health (structural fingerprint similarity layer)."""
    from redibis.profiling.vector_store import VectorConfig, get_vector_store

    gs = get_config_store().load_global_settings()
    cfg = VectorConfig.from_env(gs)
    store = get_vector_store(global_settings=gs)
    return {"config": cfg.__dict__, "health": store.health()}


@app.get("/api/columns/{table}/{column}/similar")
async def column_similar(table: str, column: str, k: int = 5) -> dict:
    """Find nearest golden columns by structural fingerprint similarity."""
    from redibis.profiling.fingerprint import ColumnFingerprint
    from redibis.profiling.inference import infer_column_governance
    from redibis.store.fingerprint_store import FingerprintStore

    fp_store = FingerprintStore.from_env(get_contract_store().backend, get_contract_store().bucket)
    raw = fp_store.get_column(table, column)
    if not raw:
        raise HTTPException(status_code=404, detail=f"No fingerprint for {table}.{column}")
    fp = ColumnFingerprint.from_dict(raw)
    matches = infer_column_governance(fp, k=k)
    return {
        "table": table,
        "column": column,
        "matches": [m.to_dict() for m in matches],
    }


@app.get("/api/golden")
async def list_golden(q: Optional[str] = None) -> dict:
    """Browse the golden reference set."""
    from redibis.store.golden_store import GoldenStore

    gs = GoldenStore.from_env(get_contract_store().backend, get_contract_store().bucket)
    cols = gs.list_all()
    if q:
        ql = q.lower()
        cols = [
            c for c in cols
            if ql in c.column_name.lower() or ql in c.table_name.lower()
            or (c.glossary and ql in c.glossary.lower())
        ]
    return {"count": len(cols), "columns": [c.to_dict() for c in cols]}


@app.post("/api/vector/reload")
async def vector_reload() -> dict:
    """Reload golden index from durable storage."""
    from redibis.profiling.vector_store import VectorConfig, get_vector_store, reset_vector_store

    gs = get_config_store().load_global_settings()
    reset_vector_store()
    store = get_vector_store(global_settings=gs, force_rebuild=True)
    return {"status": "reloaded", "health": store.health()}


@app.get("/api/contracts/{table}/memory/hints")
async def memory_hints(table: str) -> dict:
    """Read-only similar past steward reviews for enrichment UI hints."""
    mc = get_contract_store().memory_config
    if not mc.enabled:
        return {"enabled": False, "table": table, "columns": []}
    active = get_contract_store().get_active(table)
    if active is None:
        raise HTTPException(status_code=404, detail=f"No active contract for '{table}'")
    return {
        "enabled": True,
        "table": table,
        "columns": _memory_hints_for_contract(table, active),
    }


@app.get("/api/contracts/{table}/pii-decisions")
async def get_pii_decisions(table: str) -> dict:
    """Return the per-column PII state + the decision overlay for a table.

    `columns` lists every schema column with whether it currently reads as PII,
    so the UI can offer strip (for PII columns) and add (for non-PII columns).
    """
    active = get_contract_store().get_active(table)
    if active is None:
        raise HTTPException(status_code=404, detail=f"No active contract for '{table}'")
    decisions = get_contract_store().get_pii_decisions(table)
    columns = []
    for schema_obj in active.get("schema", []) or []:
        for prop in schema_obj.get("properties", []) or []:
            if not isinstance(prop, dict):
                continue
            name = prop.get("name")
            columns.append({
                "column": name,
                "is_pii": _column_is_pii(prop),
                "entity_type": (prop.get("pii") or {}).get("entity_type"),
                "classification": prop.get("classification"),
                "decision": decisions.get(name),
            })
    return {"table": table, "columns": columns,
            "decisions": get_contract_store().pii_decisions.list(table)}


@app.post("/api/contracts/{table}/columns/{column}/strip-pii")
async def strip_pii_column(table: str, column: str, body: StripPiiBody) -> dict:
    """Remove all PII metadata from a column → a normal business column."""
    try:
        res = get_contract_store().set_pii_decision(
            table, column, "not_pii",
            decided_by=body.decided_by, run_id=body.run_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "stripped", "table": table, "column": column,
            "version_after": res.version_after}


@app.post("/api/contracts/{table}/columns/{column}/add-pii")
async def add_pii_column(table: str, column: str, body: AddPiiColumnBody) -> dict:
    """Mark an existing (non-scanned) column as PII for the given entity type."""
    from redibis.services.session_service import pii_row_to_fragment
    frag = pii_row_to_fragment({
        "column": column, "detected": True, "entity_type": body.entity_type,
        "confidence": body.confidence, "arabic_aware": body.arabic_aware,
    })
    try:
        res = get_contract_store().set_pii_decision(
            table, column, "pii", entity_type=body.entity_type, payload=frag,
            decided_by=body.decided_by, run_id=body.run_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "added", "table": table, "column": column,
            "entity_type": body.entity_type, "version_after": res.version_after}


@app.delete("/api/contracts/{table}/columns/{column}/pii-decision")
async def clear_pii_decision(table: str, column: str) -> dict:
    """Stop enforcing a column's PII decision (existing metadata is unchanged)."""
    removed = get_contract_store().clear_pii_decision(table, column)
    return {"status": "cleared" if removed else "noop", "table": table, "column": column}


# ── Scoped contract views (PII / quality / definitions) ─────────────────────

class PatchPrivacyBody(BaseModel):
    entity_type: Optional[str] = None
    classification: Optional[str] = None
    tags: Optional[list[str]] = None
    masking_policy: Optional[dict[str, Any]] = None
    strip: bool = False
    decided_by: str = "web"
    run_id: str = ""


class QualitySuppressBody(BaseModel):
    column: Optional[str] = None
    decided_by: str = "web"
    run_id: str = ""


class QualityManualBody(BaseModel):
    rule: dict[str, Any]
    rule_id: Optional[str] = None
    column: Optional[str] = None
    source_rule_id: Optional[str] = None
    suppress_source: bool = False
    decided_by: str = "web"
    run_id: str = ""


class DefinitionsPatchBody(BaseModel):
    table: Optional[dict[str, Any]] = None
    columns: Optional[dict[str, dict[str, Any]]] = None
    decided_by: str = "web"
    run_id: str = ""


@app.get("/api/contracts/{table}/pii-view")
async def get_pii_view(table: str) -> dict:
    try:
        return get_contract_store().get_pii_view(table)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.patch("/api/contracts/{table}/columns/{column}/privacy")
async def patch_column_privacy(table: str, column: str, body: PatchPrivacyBody) -> dict:
    try:
        res = get_contract_store().patch_column_privacy(
            table, column,
            entity_type=body.entity_type,
            classification=body.classification,
            tags=body.tags,
            masking_policy=body.masking_policy,
            strip=body.strip,
            decided_by=body.decided_by,
            run_id=body.run_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "updated", "table": table, "column": column,
            "version_after": res.version_after}


@app.get("/api/contracts/{table}/quality-view")
async def get_quality_view(table: str) -> dict:
    try:
        return get_contract_store().get_quality_view(table)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/api/contracts/{table}/quality-decisions/{rule_id}/suppress")
async def suppress_quality_rule(table: str, rule_id: str, body: QualitySuppressBody) -> dict:
    try:
        res = get_contract_store().suppress_quality_rule(
            table, rule_id, column=body.column,
            decided_by=body.decided_by, run_id=body.run_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "suppressed", "rule_id": rule_id, "version_after": res.version_after}


@app.post("/api/contracts/{table}/quality-decisions/suppress-all")
async def suppress_all_quality_rules(table: str, body: QualitySuppressBody) -> dict:
    try:
        res = get_contract_store().suppress_all_quality_rules(
            table, decided_by=body.decided_by, run_id=body.run_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "suppressed_all", "version_after": res.version_after}


@app.post("/api/contracts/{table}/quality-decisions/{rule_id}/restore")
async def restore_quality_rule(table: str, rule_id: str) -> dict:
    try:
        res = get_contract_store().restore_quality_rule(table, rule_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "restored", "rule_id": rule_id, "version_after": res.version_after}


@app.post("/api/contracts/{table}/quality-decisions/manual")
async def add_manual_quality_rule(table: str, body: QualityManualBody) -> dict:
    from redibis.services.session_service import quality_row_to_fragment
    payload = quality_row_to_fragment({**body.rule, "column": body.column})
    try:
        res = get_contract_store().add_manual_quality_rule(
            table, payload,
            rule_id=body.rule_id,
            column=body.column,
            source_rule_id=body.source_rule_id,
            suppress_source=body.suppress_source,
            decided_by=body.decided_by,
            run_id=body.run_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "manual", "version_after": res.version_after}


@app.get("/api/contracts/{table}/definitions-view")
async def get_definitions_view(table: str) -> dict:
    try:
        return get_contract_store().get_definitions_view(table)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.patch("/api/contracts/{table}/definitions")
async def patch_definitions(table: str, body: DefinitionsPatchBody) -> dict:
    try:
        res = get_contract_store().patch_definitions(
            table,
            table_patch=body.table,
            column_patches=body.columns,
            decided_by=body.decided_by,
            run_id=body.run_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "updated", "version_after": res.version_after}


# ── v2: Runs (select-and-merge) ──────────────────────────────────────────────

def _check_kind(kind: str) -> None:
    if kind not in VALID_KINDS:
        raise HTTPException(status_code=400,
                            detail=f"kind must be one of {VALID_KINDS}, got {kind!r}")


class RunEditBody(BaseModel):
    payload: dict[str, Any]
    note: str = ""
    edited_by: str = "system"


class RunMergeBody(BaseModel):
    validate_contract: bool = True


@app.get("/api/contracts/{table}/runs")
async def list_runs(table: str, kind: str) -> dict:
    """List the run subcontracts for a table in the pii or quality bucket."""
    _check_kind(kind)
    return {"table": table, "kind": kind,
            "runs": get_subcontract_store().list_run_summaries(kind, table)}


@app.get("/api/runs/{kind}/{table}/{run_id}")
async def get_run(kind: str, table: str, run_id: str) -> dict:
    _check_kind(kind)
    sub = get_run_merger().get_run(kind, table, run_id)
    if sub is None:
        raise HTTPException(status_code=404, detail=f"No {kind} run {run_id!r} for {table!r}")
    return sub.to_dict()


@app.patch("/api/runs/{kind}/{table}/{run_id}")
async def edit_run(kind: str, table: str, run_id: str, body: RunEditBody) -> dict:
    _check_kind(kind)
    sub = get_run_merger().edit_run(kind, table, run_id, body.payload,
                               note=body.note, edited_by=body.edited_by)
    if sub is None:
        raise HTTPException(status_code=404, detail=f"No {kind} run {run_id!r} for {table!r}")
    return sub.to_dict()


@app.post("/api/runs/{kind}/{table}/{run_id}/merge")
async def merge_run(kind: str, table: str, run_id: str, body: RunMergeBody) -> dict:
    _check_kind(kind)
    try:
        result = store_op(
            get_run_merger().merge_run, kind, table, run_id, validate=body.validate_contract
        )
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Merge failed: {e}")
    return {"status": "merged", **result.to_dict()}


@app.post("/api/runs/{kind}/{table}/{run_id}/discard")
async def discard_run(kind: str, table: str, run_id: str) -> dict:
    _check_kind(kind)
    sub = get_run_merger().discard_run(kind, table, run_id)
    if sub is None:
        raise HTTPException(status_code=404, detail=f"No {kind} run {run_id!r} for {table!r}")
    return {"status": "discarded", **sub.summary()}


# ── v2: Purge (admin) ─────────────────────────────────────────────────────────

@app.delete("/api/contracts/{table}")
async def purge_contract(table: str, keep_runs: bool = True) -> dict:
    """Purge the active contract + all audits/business/enrichment → fresh UUID."""
    result = get_contract_store().purge(table)
    if not keep_runs:
        result["deleted_runs"] = get_subcontract_store().delete_table_runs(table)
    return {"status": "purged", **result}


# ── v2: Rules round-trip ───────────────────────────────────────────────────────

@app.get("/api/contracts/{table}/rules")
async def get_contract_rules(table: str) -> dict:
    from redibis.contracts.rules import extract_rules
    active = get_contract_store().get_active(table)
    if active is None:
        raise HTTPException(status_code=404, detail=f"No active contract for '{table}'")
    return {"table": table, "rules": [r.to_dict() for r in extract_rules(active)]}


@app.get("/api/contracts/{table}/rules/export")
async def export_contract_rules(table: str, target: str = "ge"):
    from redibis.contracts.rules import regenerate
    active = get_contract_store().get_active(table)
    if active is None:
        raise HTTPException(status_code=404, detail=f"No active contract for '{table}'")
    try:
        out = regenerate(active, target)
    except ImportError as e:
        raise HTTPException(status_code=501, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if isinstance(out, dict):
        return JSONResponse(out)
    return HTMLResponse(content=out, media_type="text/plain")


# ── v2: Enrichment (full-contract, validity-gated) ─────────────────────────────

class EnrichBody(BaseModel):
    provider: str = Field(default_factory=_default_llm_provider)
    model: Optional[str] = None
    system_prompt: Optional[str] = None
    extra_instructions: Optional[str] = None
    api_key: Optional[str] = None
    endpoint_url: Optional[str] = None
    example_contracts: Optional[list[str]] = None
    enriched_by: str = "system"
    external_masked_acknowledged: bool = False
    bypass_rai: bool = False
    run_id: Optional[str] = None
    use_context_draft: bool = False


class EnrichContextBody(BaseModel):
    """Editable enrichment context bundle (per-run draft)."""
    run_id: Optional[str] = None
    contract_det: Optional[dict] = None
    columns: Optional[list[dict]] = None
    instructions: Optional[dict] = None
    docs: Optional[dict] = None
    memory_hints: Optional[list[dict]] = None
    global_: Optional[dict] = Field(default=None, alias="global")


class EnrichContextPreviewBody(BaseModel):
    example_contracts: Optional[list[str]] = None


class EnrichContextRunBody(EnrichBody):
    """Run enrichment with the saved (or inline) context bundle."""
    context: Optional[dict] = None


class EnrichContextPromoteBody(BaseModel):
    column: str
    from_entity: str = "UNKNOWN"
    to_entity: str = ""
    note: str = ""
    policy_pack: str = "telecom"


def _enrichment_service():
    from redibis.enrich.service import enrichment_service_for_store

    return enrichment_service_for_store(get_contract_store())


@app.get("/api/llm-providers")
async def llm_providers() -> dict:
    """List the LLM providers defined in llm_providers.json (LiteLLM-backed)."""
    from redibis.enrich.providers import list_providers, user_provider_config_path
    return {
        "providers": list_providers(),
        "registry_path": str(user_provider_config_path()),
        "default_provider": _default_llm_provider(),
    }


@app.get("/api/llm-providers/registry")
async def llm_providers_registry() -> dict:
    """Full provider registry for editing (never returns literal api_key)."""
    from redibis.enrich.providers import load_provider_configs, user_provider_config_path
    cfgs = load_provider_configs()
    safe = {}
    for name, cfg in cfgs.items():
        row = {k: v for k, v in cfg.items() if k != "api_key"}
        row["api_key_saved"] = bool(cfg.get("api_key"))
        safe[name] = row
    return {"path": str(user_provider_config_path()), "providers": safe}


class LlmProvidersRegistryBody(BaseModel):
    providers: dict = {}


@app.put("/api/llm-providers/registry")
async def llm_providers_registry_save(body: LlmProvidersRegistryBody) -> dict:
    """Save user provider registry (api_key_env only — never raw secrets)."""
    from redibis.enrich.providers import EnrichmentError, save_user_provider_configs
    try:
        path = save_user_provider_configs(body.providers or {})
    except EnrichmentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "path": path}


@app.get("/api/llm/calls")
async def llm_calls(
    limit: int = Query(50, ge=1, le=200),
    model_role: str = Query(""),
    run_id: str = Query(""),
    routing_revision: Optional[int] = Query(None),
) -> dict:
    """Recent LLM call records (in-memory ring buffer)."""
    from redibis.enrich.llm_logging import get_recent_llm_calls

    return {
        "calls": get_recent_llm_calls(
            limit=limit,
            model_role=model_role,
            run_id=run_id,
            routing_revision=routing_revision,
        )
    }


class LlmTestBody(BaseModel):
    model: Optional[str] = None
    api_key: Optional[str] = None
    endpoint_url: Optional[str] = None
    prompt: Optional[str] = None
    timeout: float = Field(20.0, ge=1.0, le=60.0)


class LlmRoutesBody(BaseModel):
    schema_version: int = 1
    settings: dict[str, Any] = Field(default_factory=dict)


def _credential_ready_map() -> dict[str, bool]:
    import os
    from redibis.enrich.providers import load_provider_configs

    out: dict[str, bool] = {}
    for name, cfg in (load_provider_configs() or {}).items():
        if not isinstance(cfg, dict):
            continue
        env_name = str(cfg.get("api_key_env") or "").strip()
        if not env_name:
            out[str(name)] = True
            continue
        ready = bool(os.getenv(env_name))
        if str(name).lower() == "gemini" and not ready:
            ready = bool(os.getenv("GOOGLE_API_KEY"))
        out[str(name)] = ready
    return out


def _parse_if_match_revision(request: Request) -> Optional[int]:
    raw = (request.headers.get("if-match") or "").strip()
    if not raw:
        return None
    # Accept: revision:42 | "revision:42" | 42
    cleaned = raw.strip().strip('"').strip("'")
    if cleaned.lower().startswith("revision:"):
        cleaned = cleaned.split(":", 1)[1].strip()
    try:
        return int(cleaned)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="If-Match must be revision:<int>") from exc


@app.get("/api/llm/routes")
async def get_llm_routes() -> dict:
    """Versioned editable capability-routing envelope."""
    from redibis.enrich.capability_routing import routes_public_envelope

    gs = get_config_store().load_global_settings()
    return routes_public_envelope(gs)


@app.put("/api/llm/routes")
async def put_llm_routes(body: LlmRoutesBody, request: Request) -> dict:
    """Atomic save of capability routes with optimistic concurrency."""
    from redibis.enrich.capability_routing import RoutingError, apply_routes_put, routes_public_envelope

    expected = _parse_if_match_revision(request)
    gs = get_config_store().load_global_settings()
    try:
        merged = apply_routes_put(
            gs,
            {"schema_version": body.schema_version, "settings": body.settings or {}},
            expected_revision=expected,
        )
    except RoutingError as exc:
        msg = str(exc)
        if "revision mismatch" in msg:
            raise HTTPException(status_code=409, detail=msg) from exc
        raise HTTPException(status_code=400, detail=msg) from exc
    if _settings_contains_secret_key(merged.get("llm") or {}):
        raise HTTPException(
            status_code=400,
            detail="secret-bearing settings must use environment or provider credential references",
        )
    get_config_store().save_global_settings(merged)
    return {"status": "saved", "routes": routes_public_envelope(merged)}


@app.post("/api/llm/routes/validate")
async def validate_llm_routes(body: LlmRoutesBody) -> dict:
    """Dry-run validation of a routes envelope (no persist)."""
    from redibis.enrich.capability_routing import (
        RoutingError,
        build_bindings_from_global,
        role_overrides_from_llm_block,
        validate_role_overrides,
    )

    settings = body.settings or {}
    llm = settings.get("llm") if isinstance(settings.get("llm"), dict) else {}
    try:
        validate_role_overrides(role_overrides_from_llm_block(llm))
        # Also ensure merged view with current legacy keys is valid.
        gs = dict(get_config_store().load_global_settings())
        gs["llm"] = llm
        build_bindings_from_global(gs, agents_cfg=_redibis_config().agents)
    except RoutingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


@app.get("/api/llm/routes/runtime")
async def get_llm_routes_runtime() -> dict:
    """Redacted effective role matrix for new runs."""
    from redibis.enrich.capability_routing import runtime_routes_matrix

    gs = get_config_store().load_global_settings()
    return runtime_routes_matrix(
        gs,
        agents_cfg=_redibis_config().agents,
        credential_status=_credential_ready_map(),
    )


@app.post("/api/llm/routes/{role}/test")
async def test_llm_route(role: str, body: Optional[LlmTestBody] = None) -> dict:
    """Staged connectivity probe for one capability role (never persists keys)."""
    from redibis.enrich.capability_routing import (
        RoutingError,
        assert_residency_allowed,
        build_bindings_from_global,
        known_roles,
        resolve_model_binding,
    )
    from redibis.enrich.probe import probe_provider
    from redibis.enrich.providers import get_provider_config

    if role not in known_roles():
        raise HTTPException(status_code=404, detail=f"unknown role {role!r}")
    gs = get_config_store().load_global_settings()
    bindings = build_bindings_from_global(gs, agents_cfg=_redibis_config().agents)
    try:
        binding = resolve_model_binding(role, bindings=bindings)
    except RoutingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not binding.enabled:
        return {"ok": False, "role": role, "error": "role disabled", "binding": binding.to_dict()}
    if not binding.provider:
        return {
            "ok": False,
            "role": role,
            "error": "no provider bound (heuristic/template fallback may apply)",
            "binding": binding.to_dict(),
        }
    try:
        cfg = get_provider_config(binding.provider)
        assert_residency_allowed(binding, str(cfg.get("residency") or "local"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    test_body = body or LlmTestBody()
    result = probe_provider(
        binding.provider,
        model=test_body.model or binding.model or "",
        api_key=test_body.api_key,
        endpoint_url=test_body.endpoint_url,
    )
    result["role"] = role
    result["binding"] = binding.to_dict()
    return result


@app.get("/api/llm-providers/{name}/env-status")
async def llm_provider_env_status(name: str) -> dict:
    """Report whether the configured api_key_env var is set (never the value)."""
    from redibis.enrich.providers import get_provider_config
    import os

    try:
        cfg = get_provider_config(name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    env_name = cfg.get("api_key_env") or ""
    key = (name or "").lower()
    env_set = bool(env_name and os.getenv(env_name))
    if key == "gemini" and not env_set:
        env_set = bool(os.getenv("GOOGLE_API_KEY"))
    return {
        "provider": name.lower(),
        "api_key_env": env_name,
        "set": env_set,
    }


@app.post("/api/llm-providers/{name}/test")
async def llm_provider_test(name: str, body: LlmTestBody) -> dict:
    """Run a bounded connectivity probe against one LLM provider (ephemeral key)."""
    from redibis.enrich.probe import probe_provider

    return probe_provider(
        name,
        model=body.model or "",
        api_key=body.api_key,
        endpoint_url=body.endpoint_url,
        prompt=body.prompt,
        timeout=body.timeout,
    )


# ── Custom provider profiles (Enrich UI) ─────────────────────────────────────


class LlmProfileBody(BaseModel):
    name: str = ""
    description: str = ""
    profile_type: str = "openai_compatible"  # openai_compatible | raw
    litellm_model: str = ""
    model: str = ""
    model_prefix: str = "openai"
    api_base: Optional[str] = None
    api_key_env: str = ""
    supports_json: bool = True
    residency: str = "local"
    params: dict = {}
    known_models: list = []
    allow_override_packaged: bool = False


class LlmProfileTestBody(BaseModel):
    """Test a saved name or an unsaved inline profile (session key never persisted)."""
    name: Optional[str] = None
    profile: Optional[dict] = None
    api_key: Optional[str] = None  # session-only
    timeout: float = Field(20.0, ge=1.0, le=60.0)


@app.get("/api/llm/providers")
async def llm_providers_v2() -> dict:
    """Merged provider list with source/editable flags for Enrich custom profiles."""
    from redibis.enrich.provider_profiles import (
        GENERIC_OPENAI_PRESET,
        SGLANG_QWEN_PRESET,
        list_profiles,
    )
    from redibis.enrich.providers import user_provider_config_path

    return {
        "providers": list_profiles(),
        "registry_path": str(user_provider_config_path()),
        "default_provider": _default_llm_provider(),
        "presets": {
            "sglang_qwen": SGLANG_QWEN_PRESET,
            "openai_compatible": GENERIC_OPENAI_PRESET,
        },
    }


@app.post("/api/llm/providers")
async def llm_provider_create(body: LlmProfileBody) -> dict:
    """Create or update a kind=custom provider profile (keys never persisted)."""
    from redibis.enrich.provider_profiles import save_profile
    from redibis.enrich.providers import EnrichmentError

    try:
        return save_profile(
            body.model_dump(),
            allow_override_packaged=bool(body.allow_override_packaged),
        )
    except EnrichmentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/llm/providers/{name}")
async def llm_provider_delete(name: str) -> dict:
    """Delete a kind=custom profile only."""
    from redibis.enrich.provider_profiles import delete_profile
    from redibis.enrich.providers import EnrichmentError

    try:
        return delete_profile(name)
    except EnrichmentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/llm/providers/test")
async def llm_provider_staged_test(body: LlmProfileTestBody) -> dict:
    """Staged connectivity test (models → completion → json_mode) with raw errors."""
    from redibis.enrich.provider_profiles import test_profile

    target: object
    if body.profile and isinstance(body.profile, dict):
        target = dict(body.profile)
        if body.name and not target.get("name"):
            target["name"] = body.name
    elif body.name:
        target = body.name
    else:
        raise HTTPException(status_code=400, detail="Provide name or profile payload")

    return test_profile(
        target,
        session_key=body.api_key,
        timeout=float(body.timeout or 20.0),
    )


@app.get("/api/llm/providers/{name}/models")
async def llm_provider_remote_models(
    name: str,
    endpoint_url: Optional[str] = Query(None),
) -> dict:
    """Proxy GET {api_base}/models for a saved profile (SSRF-guarded)."""
    from redibis.enrich.provider_profiles import (
        _models_fetch_allow_private,
        fetch_remote_models,
    )
    from redibis.enrich.providers import EnrichmentError, load_provider_configs

    cfgs = load_provider_configs()
    key = (name or "").strip().lower()
    cfg = cfgs.get(key)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"Unknown provider {name!r}")
    api_base = (endpoint_url or "").strip() or cfg.get("api_base") or ""
    if not api_base:
        raise HTTPException(
            status_code=400,
            detail=f"Provider {key!r} has no api_base to query for models",
        )
    try:
        return fetch_remote_models(
            api_base,
            session_key=None,
            allow_private=_models_fetch_allow_private(cfg, api_base),
        )
    except EnrichmentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/enrich/default-prompt")
async def enrich_default_prompt() -> dict:
    """The editable default system prompt (starting point for prompt engineering)."""
    from redibis.enrich.service import EnrichmentService
    return {"system_prompt": EnrichmentService.default_system_prompt()}


@app.get("/api/enrich/{table}/context")
async def get_enrichment_context(table: str, run_id: Optional[str] = None) -> dict:
    """Assembled enrichment-context bundle: C_det + evidence + instructions."""
    from redibis.enrich.context import assemble_enrichment_context

    svc = _enrichment_service()
    try:
        return assemble_enrichment_context(
            svc, table, redibis_config=_redibis_config(), run_id=run_id,
        )
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.put("/api/enrich/{table}/context")
async def put_enrichment_context(table: str, body: EnrichContextBody) -> dict:
    """Save human edits to the per-run enrichment context draft."""
    from redibis.enrich.context import assemble_enrichment_context, save_context_draft

    svc = _enrichment_service()
    try:
        base = assemble_enrichment_context(svc, table, redibis_config=_redibis_config())
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    bundle = dict(base)
    if body.run_id:
        bundle["run_id"] = body.run_id
    if body.contract_det is not None:
        bundle["contract_det"] = body.contract_det
    if body.columns is not None:
        bundle["columns"] = body.columns
    if body.instructions is not None:
        bundle["instructions"] = {**(bundle.get("instructions") or {}), **body.instructions}
    if body.docs is not None:
        bundle["docs"] = {**(bundle.get("docs") or {}), **body.docs}
    if body.memory_hints is not None:
        bundle["memory_hints"] = body.memory_hints
    if body.global_ is not None:
        bundle["global"] = body.global_
    key = save_context_draft(svc.store, table, bundle)
    return {"status": "saved", "key": key, "context": bundle}


@app.post("/api/enrich/{table}/context/preview")
async def preview_enrichment_context(
    table: str, body: Optional[EnrichContextPreviewBody] = None,
) -> dict:
    """Render the exact prompt/context that would be sent (no LLM call)."""
    from redibis.enrich.context import (
        assemble_enrichment_context,
        load_context_draft,
        preview_enrichment_prompt,
    )

    svc = _enrichment_service()
    bundle = load_context_draft(svc.store, table)
    if bundle is None:
        try:
            bundle = assemble_enrichment_context(svc, table, redibis_config=_redibis_config())
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
    ex = (body.example_contracts if body else None)
    return preview_enrichment_prompt(
        svc, bundle, redibis_config=_redibis_config(), example_contracts=ex,
    )


@app.post("/api/enrich/{table}/context/promote")
async def promote_enrichment_context_rule(table: str, body: EnrichContextPromoteBody) -> dict:
    """Promote a steward correction to a suggested edge-rule (Settings bridge)."""
    from redibis.enrich.context import promote_column_to_edge_rule

    suggestion = promote_column_to_edge_rule(
        column=body.column,
        from_entity=body.from_entity,
        to_entity=body.to_entity,
        note=body.note,
    )
    return {
        "table": table,
        "column": body.column,
        "suggested_rule": suggestion,
        "policy_pack": body.policy_pack,
        "hint": "Persist via POST /api/classification/edge-rules/promote or Settings pack editor.",
    }


@app.post("/api/enrich/{table}/run")
async def run_enrichment_with_context(table: str, body: EnrichContextRunBody) -> dict:
    """Run enrichment with the edited context bundle → auto-write + run artifacts."""
    from datetime import datetime, timezone

    from redibis.enrich.context import load_context_draft
    from redibis.store.run_output_writer import RunOutputWriter

    svc = _enrichment_service()
    store = get_contract_store()
    bundle = body.context or load_context_draft(store, table)
    run_id = body.run_id or (bundle or {}).get("run_id") or datetime.now(timezone.utc).strftime(
        "%Y-%m-%d_%H-%M-%S",
    )
    cfg = _redibis_config()
    run_writer = RunOutputWriter(
        backend=store.backend,
        bucket=cfg.storage.runs_bucket,
        workflow="enrich",
        table=table,
        run_id=run_id,
    )
    try:
        provider, _binding = _provider_for_role(
            "contract.enrichment",
            provider=body.provider or "",
            model=body.model or "",
            api_key=body.api_key,
            endpoint_url=body.endpoint_url,
        )
        result = svc.enrich(
            table, provider,
            system_prompt=body.system_prompt,
            extra_instructions=body.extra_instructions,
            example_contracts=body.example_contracts,
            enriched_by=body.enriched_by,
            external_masked_acknowledged=body.external_masked_acknowledged,
            bypass_rai=body.bypass_rai,
            redibis_config=cfg,
            run_writer=run_writer,
            run_id=run_id,
            context_bundle=bundle,
        )
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception("Context enrichment failed for table %r", table)
        raise HTTPException(status_code=502, detail=f"Enrichment failed: {e}")
    return result.to_dict()


# ── Contract Synthesis (portable ODCS v3.1 — no upsert) ────────────────────

@app.post("/api/synthesis/{table}/run")
async def synthesis_run(
    table: str,
    analysis_mode: str = Form("deterministic"),
    odcs_version: str = Form("v3.1.0"),
    output_dir: str = Form(""),
    provider: str = Form(""),
    compare_modes: str = Form("false"),
    files: list[UploadFile] = File(default=[]),
) -> dict:
    """
    Run Contract Synthesis for ``table``.

    Multipart: optional file uploads (requirements, sources, ZIP).
    Base contract is loaded from the active store.
    Never writes through ContractStore.upsert — portable artifacts only.
    """
    from redibis.synthesis import ContractSynthesisRunner

    uploaded: dict[str, bytes] = {}
    for f in files or []:
        raw = await f.read()
        uploaded[f.filename or f"upload-{len(uploaded)}"] = raw

    store = _contract_store()
    try:
        base = store.get_active(table)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"no active contract: {exc}") from exc
    if not base:
        raise HTTPException(status_code=404, detail=f"no active contract for {table}")

    provider_obj = None
    want_assisted = (analysis_mode or "").lower() == "assisted"
    want_compare = str(compare_modes).lower() in ("1", "true", "yes")
    if want_assisted or want_compare:
        try:
            from redibis.enrich.providers import get_provider
            provider_obj = get_provider(provider or "demo")
        except Exception as exc:
            if want_assisted:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

    out = output_dir or f"./synthesis_out/{table.replace('.', '_')}"
    runner = ContractSynthesisRunner(
        analysis_mode=analysis_mode or "deterministic",
        odcs_version=odcs_version or "v3.1.0",
        provider=provider_obj,
        redibis_config=_redibis_config(),
    )
    try:
        result = runner.run(
            base_contract=base,
            uploaded=uploaded or None,
            output_dir=out,
            also_run_assisted_compare=want_compare,
        )
    except Exception as exc:
        logger.exception("Contract Synthesis failed for %r", table)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        **result.to_dict(),
        "lineage_react_flow": result.lineage_react_flow,
        "candidate_preview": {
            "apiVersion": (result.candidate or {}).get("apiVersion"),
            "name": (result.candidate or {}).get("name"),
            "version": (result.candidate or {}).get("version"),
            "status": (result.candidate or {}).get("status"),
            "property_count": sum(
                len(s.get("properties") or [])
                for s in ((result.candidate or {}).get("schema") or [])
            ),
        },
    }


@app.post("/api/synthesis/run-file")
async def synthesis_run_file(
    contract: UploadFile = File(...),
    analysis_mode: str = Form("deterministic"),
    odcs_version: str = Form("v3.1.0"),
    output_dir: str = Form("./synthesis_out"),
    provider: str = Form(""),
    files: list[UploadFile] = File(default=[]),
) -> dict:
    """Synthesis from an uploaded base contract file + optional docs/sources."""
    import yaml as _yaml
    from redibis.synthesis import ContractSynthesisRunner

    raw_contract = await contract.read()
    try:
        base = _yaml.safe_load(raw_contract.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid contract YAML: {exc}") from exc
    if not isinstance(base, dict):
        raise HTTPException(status_code=400, detail="contract must be a YAML mapping")

    uploaded: dict[str, bytes] = {}
    for f in files or []:
        uploaded[f.filename or f"upload-{len(uploaded)}"] = await f.read()

    provider_obj = None
    if (analysis_mode or "").lower() == "assisted":
        from redibis.enrich.providers import get_provider
        try:
            provider_obj = get_provider(provider or "demo")
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    runner = ContractSynthesisRunner(
        analysis_mode=analysis_mode or "deterministic",
        odcs_version=odcs_version or "v3.1.0",
        provider=provider_obj,
        redibis_config=_redibis_config(),
    )
    try:
        result = runner.run(
            base_contract=base,
            uploaded=uploaded or None,
            output_dir=output_dir or "./synthesis_out",
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        **result.to_dict(),
        "lineage_react_flow": result.lineage_react_flow,
    }


@app.get("/api/synthesis/{table}/graph")
async def synthesis_graph(table: str, output_dir: str = "") -> dict:
    """Load the last lineage React Flow graph from an output directory."""
    from pathlib import Path
    import json as _json

    root = Path(output_dir or f"./synthesis_out/{table.replace('.', '_')}")
    path = root / "lineage_graph.json"
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"no lineage graph at {path}")
    return _json.loads(path.read_text(encoding="utf-8"))


@app.post("/api/contracts/{table}/context-docs")
async def upload_context_doc(table: str, file: UploadFile = File(...)) -> dict:
    svc = _enrichment_service()
    content = await file.read()
    key = svc.add_context_doc(table, file.filename or "context.txt", content)
    return {"status": "uploaded", "key": key, "docs": svc.list_context_docs(table)}


@app.get("/api/contracts/{table}/context-docs")
async def list_context_docs(table: str) -> dict:
    return {"table": table, "docs": _enrichment_service().list_context_docs(table)}


@app.delete("/api/contracts/{table}/context-docs/{filename}")
async def delete_context_doc(table: str, filename: str) -> dict:
    svc = _enrichment_service()
    if not svc.delete_context_doc(table, filename):
        raise HTTPException(status_code=404, detail=f"No context doc {filename!r}")
    return {"status": "deleted", "docs": svc.list_context_docs(table)}


@app.post("/api/contracts/{table}/example-docs")
async def upload_example_doc(table: str, file: UploadFile = File(...)) -> dict:
    svc = _enrichment_service()
    content = await file.read()
    key = svc.add_example_doc(table, file.filename or "example.txt", content)
    return {"status": "uploaded", "key": key, "docs": svc.list_example_docs(table)}


@app.get("/api/contracts/{table}/example-docs")
async def list_example_docs(table: str) -> dict:
    return {"table": table, "docs": _enrichment_service().list_example_docs(table)}


@app.delete("/api/contracts/{table}/example-docs/{filename}")
async def delete_example_doc(table: str, filename: str) -> dict:
    svc = _enrichment_service()
    if not svc.delete_example_doc(table, filename):
        raise HTTPException(status_code=404, detail=f"No example doc {filename!r}")
    return {"status": "deleted", "docs": svc.list_example_docs(table)}


@app.post("/api/contracts/{table}/sample-data")
async def upload_sample_data(table: str, file: UploadFile = File(...)) -> dict:
    """Upload de-identified sample rows (CSV/Parquet/JSON) for LLM context."""
    svc = _enrichment_service()
    content = await file.read()
    key = svc.add_sample_data(table, file.filename or "sample.csv", content)
    return {"status": "uploaded", "key": key, "docs": svc.list_sample_data(table)}


@app.get("/api/contracts/{table}/sample-data")
async def list_sample_data(table: str) -> dict:
    return {"table": table, "docs": _enrichment_service().list_sample_data(table)}


@app.delete("/api/contracts/{table}/sample-data/{filename}")
async def delete_sample_data(table: str, filename: str) -> dict:
    svc = _enrichment_service()
    if not svc.delete_sample_data(table, filename):
        raise HTTPException(status_code=404, detail=f"No sample file {filename!r}")
    return {"status": "deleted", "docs": svc.list_sample_data(table)}


@app.get("/api/config/rai")
async def rai_config() -> dict:
    """Expose global RAI settings (read-only) for UI/ops."""
    cfg = _redibis_config().rai
    return {
        "enabled": cfg.enabled,
        "mode": cfg.mode,
        "enforce": cfg.enforce,
        "hard_block_external_pii": cfg.hard_block_external_pii,
        "block_external_raw_pii": cfg.block_external_raw_pii,
        "default_residency": cfg.default_residency,
        "allowed_models": list(cfg.allowed_models),
        "denied_models": list(cfg.denied_models),
    }


@app.get("/api/contracts/{table}/enrich/preflight")
async def enrich_preflight(
    table: str,
    provider: str = "vllm",
    model: str = "",
    endpoint_url: Optional[str] = None,
    external_masked_acknowledged: bool = False,
) -> dict:
    """RAI / residency preview before calling an external LLM."""
    from redibis.enrich.providers import get_provider
    from redibis.enrich.rai_gate import compute_enrich_rai_preflight

    active = get_contract_store().get_active(table)
    if active is None:
        raise HTTPException(status_code=404, detail=f"No active contract for '{table}'")
    svc = _enrichment_service()
    prov = get_provider(provider, model=model or None, endpoint_url=endpoint_url)
    sample_count = len(svc.list_sample_data(table))
    return compute_enrich_rai_preflight(
        contract=active,
        table=table,
        provider=prov,
        redibis_config=_redibis_config(),
        sample_data_count=sample_count,
        external_masked_acknowledged=external_masked_acknowledged,
    )


@app.post("/api/contracts/{table}/enrich")
async def enrich_contract(table: str, body: EnrichBody) -> dict:
    from datetime import datetime, timezone

    from redibis.enrich.providers import get_provider
    from redibis.store.run_output_writer import RunOutputWriter

    svc = _enrichment_service()
    store = get_contract_store()
    run_id = body.run_id or datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    cfg = _redibis_config()
    run_writer = RunOutputWriter(
        backend=store.backend,
        bucket=cfg.storage.runs_bucket,
        workflow="enrich",
        table=table,
        run_id=run_id,
    )
    try:
        provider, _binding = _provider_for_role(
            "contract.enrichment",
            provider=body.provider or "",
            model=body.model or "",
            api_key=body.api_key,
            endpoint_url=body.endpoint_url,
        )
        result = svc.enrich(table, provider, system_prompt=body.system_prompt,
                            extra_instructions=body.extra_instructions,
                            example_contracts=body.example_contracts,
                            enriched_by=body.enriched_by,
                            external_masked_acknowledged=body.external_masked_acknowledged,
                            bypass_rai=body.bypass_rai,
                            redibis_config=cfg,
                            run_writer=run_writer,
                            run_id=run_id)
    except PermissionError as e:
        from redibis.enrich.rai_gate import compute_enrich_rai_preflight
        try:
            active = get_contract_store().get_active(table)
            prov = get_provider(body.provider, model=body.model, api_key=body.api_key,
                                endpoint_url=body.endpoint_url)
            sample_count = len(svc.list_sample_data(table))
            preflight = compute_enrich_rai_preflight(
                contract=active or {},
                table=table,
                provider=prov,
                redibis_config=_redibis_config(),
                sample_data_count=sample_count,
                external_masked_acknowledged=body.external_masked_acknowledged,
            )
        except Exception:
            preflight = {}
        raise HTTPException(
            status_code=403,
            detail={
                "message": str(e),
                "rai": preflight,
            },
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception("Enrichment failed for table %r (provider=%r model=%r)",
                         table, body.provider, body.model)
        detail = f"Enrichment failed [{type(e).__name__}]: {e}".strip()
        if detail.endswith(":"):  # some exceptions have an empty message
            detail = f"{detail} (no message — see server logs for traceback)"
        raise HTTPException(
            status_code=502,
            detail=detail,
            headers={"X-Enrich-Error": type(e).__name__},
        )
    return result.to_dict()


@app.post("/api/contracts/{table}/enrich/validate")
async def validate_enrichment(table: str) -> dict:
    try:
        return _enrichment_service().validate_candidate(table)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/api/contracts/{table}/enrich/candidate")
async def get_enrichment_candidate(table: str) -> dict:
    candidate = _enrichment_service().get_candidate(table)
    if candidate is None:
        raise HTTPException(status_code=404, detail="No enrichment candidate")
    return candidate


@app.get("/api/contracts/{table}/enrich/diff")
async def get_enrichment_diff(table: str) -> dict:
    """Diff report between the input contract and the pending enrichment candidate."""
    svc = _enrichment_service()
    diff = svc.get_diff_report(table)
    if diff is None:
        raise HTTPException(status_code=404, detail="No enrichment candidate")
    try:
        validation = svc.validate_candidate(table)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    meta = (svc.get_candidate(table) or {}).get("enrichment_meta") or {}
    return {
        "table": table,
        "valid": validation["valid"],
        "errors": validation.get("errors", []),
        "warnings": validation.get("warnings", []),
        "diff_report": diff,
        "enrichment_meta": meta,
    }


@app.post("/api/contracts/{table}/enrich/merge")
async def merge_enrichment(table: str) -> dict:
    from redibis.store.merger import IdentityConflictError

    try:
        return _enrichment_service().merge_candidate(table)
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No enrichment candidate for {table!r}. "
                "Run enrichment on this table first (the candidate is cleared after a "
                "successful merge or when the contract is purged)."
            ),
        )
    except IdentityConflictError as e:
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(e),
                "hint": (
                    "The active contract identity changed after enrichment was run. "
                    "Re-run enrichment, then merge again."
                ),
            },
        )
    except ValueError as e:
        msg = str(e)
        hint = None
        if "invalid" in msg.lower() or "validation" in msg.lower():
            hint = (
                "The LLM candidate failed ODCS validation. Review errors in the diff "
                "panel and re-run enrichment, or edit definitions manually."
            )
        elif "corrupt YAML" in msg:
            hint = "The stored enrichment candidate is unreadable. Re-run enrichment."
        detail: dict | str = {"message": msg, "hint": hint} if hint else msg
        raise HTTPException(status_code=409, detail=detail)
    except Exception as e:
        logger.exception("Enrichment merge failed for table %r", table)
        detail = f"Merge failed [{type(e).__name__}]: {e}".strip()
        if detail.endswith(":"):
            detail = f"{detail} (see server logs for traceback)"
        raise HTTPException(
            status_code=502,
            detail=detail,
            headers={"X-Enrich-Error": type(e).__name__},
        )


@app.get("/api/contracts/{table}/triage")
async def get_contract_triage(table: str) -> dict:
    """Triage report — API/CLI only (decoupled from the PII verdict, not in UI)."""
    active = get_contract_store().get_active(table)
    if active is None:
        raise HTTPException(status_code=404, detail=f"No active contract for '{table}'")
    triage = []
    for schema_obj in active.get("schema", []) or []:
        for prop in schema_obj.get("properties", []) or []:
            pii = prop.get("pii", {}) or {}
            evidence = pii.get("evidence", {}) or {}
            score = evidence.get("triage_score", pii.get("triage_score"))
            if score is not None:
                triage.append({"column": prop.get("name"),
                               "triage_score": score})
    return {"table": table, "triage": triage,
            "note": "Triage is a quality-profiler signal and does not drive the PII verdict."}


@app.get("/api/contracts/{table}/html", response_class=HTMLResponse)
async def get_contract_html(table: str) -> HTMLResponse:
    active = get_contract_store().get_active(table)
    if active is None:
        raise HTTPException(status_code=404, detail=f"No active contract for '{table}'")
    import yaml as _yaml
    import html as _html
    body = _html.escape(_yaml.safe_dump(active, default_flow_style=False,
                                        sort_keys=False, allow_unicode=True))
    page = f"""<!DOCTYPE html><html><head><meta charset="utf-8"/>
<title>Contract — {_html.escape(table)}</title>
<style>body{{font-family:'IBM Plex Mono',ui-monospace,monospace;background:#0f172a;color:#e2e8f0;margin:0;padding:1.5rem}}
h1{{font-size:1rem;color:#38bdf8;margin:0 0 1rem}}pre{{white-space:pre-wrap;word-break:break-word;font-size:.8rem;line-height:1.5}}</style>
</head><body><h1>{_html.escape(table)} — v{_html.escape(str(active.get('version','')))}</h1><pre>{body}</pre></body></html>"""
    return HTMLResponse(page)


# ═══════════════════════════════════════════════════════════════════════════
# v2: Auth + scoped sharing (JSON-file users, sha256; edits apply to active)
# ═══════════════════════════════════════════════════════════════════════════

class LoginBody(BaseModel):
    username: str
    password: str


class CreateUserBody(BaseModel):
    username: str
    password: str
    role: str = "explorer"
    default_scopes: Optional[list[str]] = None


class PatchUserBody(BaseModel):
    role: Optional[str] = None
    password: Optional[str] = None


class ShareBody(BaseModel):
    scope: str = "all"                      # business | pii | quality | all
    expires_at: Optional[str] = None
    granted_to: Optional[str] = None
    created_by: str = "admin"


class ShareEditBody(BaseModel):
    edits: dict[str, Any]
    validate_contract: bool = False


def _scoped_view(active: dict, scope: str) -> dict:
    """Return only the in-scope slice of the active contract."""
    if scope == "all":
        return active
    view = {"table_name": active.get("table_name"),
            "database_name": active.get("database_name"),
            "version": active.get("version"),
            "contract_uuid": active.get("contract_uuid"),
            "scope": scope, "schema": []}
    for schema_obj in active.get("schema", []) or []:
        cols = []
        for prop in schema_obj.get("properties", []) or []:
            col = {"name": prop.get("name")}
            if scope == "business":
                col["business"] = prop.get("business")
                col["tags"] = prop.get("tags")
            elif scope == "pii":
                col["classification"] = prop.get("classification")
                col["pii"] = prop.get("pii")
            elif scope == "quality":
                col["quality"] = prop.get("quality")
            cols.append(col)
        sobj = {"name": schema_obj.get("name"),
                "physicalName": schema_obj.get("physicalName"), "properties": cols}
        if scope == "quality":
            sobj["quality"] = schema_obj.get("quality")
        if scope == "business":
            sobj["tags"] = schema_obj.get("tags")
        view["schema"].append(sobj)
    return view


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> HTMLResponse:
    user = getattr(request.state, "user", None)
    if user is not None:
        from starlette.responses import RedirectResponse

        return RedirectResponse("/", status_code=303)
    return _TEMPLATES.TemplateResponse(
        request=request,
        name="login.html",
        context=_page_context(request, error="", next=request.query_params.get("next") or "/"),
    )


@app.post("/login")
async def login_submit(request: Request):
    if not login_allowed(request):
        return JSONResponse({"detail": "too many requests"}, status_code=429)
    submitted = await csrf_from_request(request)
    content_type = (request.headers.get("content-type") or "").lower()
    wants_json = "application/json" in content_type
    if not csrf_ok(request, submitted):
        if wants_json or is_api_login(request):
            raise HTTPException(status_code=403, detail="CSRF token missing or invalid")
        return _TEMPLATES.TemplateResponse(
            request=request,
            name="login.html",
            context=_page_context(
                request, error="CSRF token missing or invalid", next="/"
            ),
            status_code=403,
        )
    if wants_json:
        body = await request.json()
        username = str((body or {}).get("username") or "").strip()
        password = str((body or {}).get("password") or "")
        nxt = str((body or {}).get("next") or "/")
    else:
        form = await request.form()
        username = str(form.get("username") or "").strip()
        password = str(form.get("password") or "")
        nxt = str(form.get("next") or "/")
    from redibis.webapp.security import safe_next

    nxt = safe_next(nxt)
    user = get_auth_store().authenticate(username, password)
    if user is None:
        if wants_json or is_api_login(request):
            raise HTTPException(status_code=401, detail=LOGIN_MESSAGE)
        return _TEMPLATES.TemplateResponse(
            request=request,
            name="login.html",
            context=_page_context(request, error=LOGIN_MESSAGE, next=nxt),
            status_code=401,
        )
    token = get_auth_store().create_session(user.username)
    if wants_json or is_api_login(request):
        resp = JSONResponse({"status": "ok", "user": user.public()})
    else:
        from starlette.responses import RedirectResponse

        resp = RedirectResponse(nxt, status_code=303)
    set_session_cookie(resp, request, token)
    return resp


def is_api_login(request: Request) -> bool:
    accept = (request.headers.get("accept") or "").lower()
    return "application/json" in accept and "text/html" not in accept


@app.post("/logout")
async def logout(request: Request):
    submitted = await csrf_from_request(request)
    if not csrf_ok(request, submitted):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid")
    get_auth_store().delete_session(session_token(request))
    accept = (request.headers.get("accept") or "").lower()
    if "text/html" in accept:
        from starlette.responses import RedirectResponse

        resp = RedirectResponse("/login", status_code=303)
    else:
        resp = JSONResponse({"status": "ok"})
    clear_session_cookie(resp)
    return resp


@app.get("/api/me")
async def me(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="authentication required")
    return {"user": user.public()}


@app.get("/api/users")
async def list_users() -> list:
    return get_auth_store().list_users()


@app.post("/api/users")
async def create_user(body: CreateUserBody) -> dict:
    try:
        user = get_auth_store().create_user(
            body.username, body.password, role=body.role,
            default_scopes=body.default_scopes)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "created", "user": user.public()}


@app.patch("/api/users/{username}")
async def patch_user(username: str, body: PatchUserBody) -> dict:
    try:
        user = None
        if body.role is not None:
            user = get_auth_store().set_role(username, body.role)
        if body.password is not None:
            user = get_auth_store().set_password(username, body.password)
        if user is None:
            user = get_auth_store().get_user(username)
            if user is None:
                raise HTTPException(status_code=404, detail="user not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "updated", "user": user.public()}


@app.delete("/api/users/{username}")
async def delete_user_route(username: str) -> dict:
    try:
        ok = get_auth_store().delete_user(username)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ok:
        raise HTTPException(status_code=404, detail="user not found")
    return {"status": "deleted", "username": username}


@app.post("/api/contracts/{table}/share")
async def create_share(table: str, body: ShareBody) -> dict:
    active = get_contract_store().get_active(table)
    if active is None:
        raise HTTPException(status_code=404, detail=f"No active contract for '{table}'")
    try:
        share = get_auth_store().create_share(
            table=table, scope=body.scope, created_by=body.created_by,
            contract_uuid=active.get("contract_uuid"),
            granted_to=body.granted_to, expires_at=body.expires_at)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "created", "share_token": share.share_token,
            "url": f"/share/{share.share_token}", **share.to_dict()}


@app.get("/api/contracts/{table}/shares")
async def list_shares(table: str) -> list:
    return get_auth_store().list_shares(table)


@app.get("/api/share/{token}")
async def get_share_view(token: str) -> dict:
    share = get_auth_store().get_share(token)
    if share is None:
        raise HTTPException(status_code=404, detail="Share not found")
    if share.is_expired():
        raise HTTPException(status_code=410, detail="Share link expired")
    active = get_contract_store().get_active(share.table)
    if active is None:
        raise HTTPException(status_code=404, detail="Contract no longer exists")
    return {"share": share.to_dict(), "scope": share.scope,
            "contract": _scoped_view(active, share.scope)}


@app.patch("/api/share/{token}")
async def edit_via_share(token: str, body: ShareEditBody) -> dict:
    """Apply scope-limited edits directly to the active contract (audited)."""
    share = get_auth_store().get_share(token)
    if share is None:
        raise HTTPException(status_code=404, detail="Share not found")
    if share.is_expired():
        raise HTTPException(status_code=410, detail="Share link expired")
    active = get_contract_store().get_active(share.table)
    if active is None:
        raise HTTPException(status_code=404, detail="Contract no longer exists")

    # Strip-PII directives: edits.columns[col].remove_pii = true. Only the
    # 'pii' and 'all' scopes may demote a column. Routed through the decision
    # overlay (not apply_scoped_edits, which can only set/union fields).
    applied_strip: list[str] = []
    if share.scope in ("pii", "all"):
        for col, ce in (body.edits.get("columns") or {}).items():
            if isinstance(ce, dict) and ce.get("remove_pii"):
                try:
                    get_contract_store().set_pii_decision(
                        share.table, col, "not_pii",
                        decided_by=f"share:{token[:8]}", run_id=f"share_{token[:8]}")
                    applied_strip.append(f"{col}.strip_pii")
                except ValueError:
                    pass  # column missing / no contract — skip silently

    # Re-fetch: a strip above may have produced a new active version.
    active = store_op(get_contract_store().get_active, share.table) or active
    modified, applied = apply_scoped_edits(active, body.edits, share.scope)
    applied = applied + applied_strip
    if not applied:
        return {"status": "noop", "applied_fields": [], "scope": share.scope}
    if not (set(applied) - set(applied_strip)):
        # Only strips happened — they're already persisted; report and return.
        return {"status": "applied", "applied_fields": applied, "scope": share.scope,
                "version_after": (store_op(get_contract_store().get_active, share.table) or {}).get("version")}
    try:
        upsert = store_op(
            get_contract_store().upsert,
            partial=modified, table=share.table,
            workflow=f"share:{share.scope}",
            run_id=f"share_{token[:8]}", validate=body.validate_contract,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Edit failed: {e}")
    return {"status": "applied", "applied_fields": applied, "scope": share.scope,
            "version_after": upsert.version_after}


@app.delete("/api/share/{token}")
async def revoke_share(token: str) -> dict:
    return {"revoked": get_auth_store().revoke_share(token), "token": token}


# ═══════════════════════════════════════════════════════════════════════════
# Health + dashboard
# ═══════════════════════════════════════════════════════════════════════════

class ActivateModelBody(BaseModel):
    name: str


# ═══════════════════════════════════════════════════════════════════════════
# NER models (bring-your-own weights)
# ═══════════════════════════════════════════════════════════════════════════

def _resolve_api_models_dir(
    *,
    session_id: Optional[str] = None,
    models_dir: Optional[str] = None,
) -> str | None:
    """API models root: explicit param → session ``pii_models_dir`` → env default."""
    if models_dir and str(models_dir).strip():
        return str(models_dir).strip()
    if session_id:
        session = _require_session(session_id)
        raw = (session.common_config.pii_models_dir or "").strip()
        if raw:
            return raw
    return None


@app.get("/api/models")
async def get_models(
    session_id: Optional[str] = Query(None),
    models_dir: Optional[str] = Query(None),
) -> dict:
    from redibis.services.model_service import list_models

    active_path = ""
    if session_id:
        session = _require_session(session_id)
        active_path = session.common_config.pii_gliner_model or ""
    base = _resolve_api_models_dir(session_id=session_id, models_dir=models_dir)
    return list_models(active_path=active_path, models_dir_base=base)


@app.post("/api/models/upload")
async def upload_model(
    file: UploadFile = File(...),
    name: Optional[str] = Form(None),
    session_id: Optional[str] = Form(None),
    models_dir: Optional[str] = Form(None),
) -> dict:
    from redibis.services.model_service import ingest_upload

    file_bytes = await file.read()
    base = _resolve_api_models_dir(session_id=session_id, models_dir=models_dir)
    result = ingest_upload(
        file_bytes,
        file.filename or "upload.zip",
        name=name,
        models_dir_base=base,
    )
    if not result.ok:
        raise HTTPException(status_code=400, detail={"errors": result.errors, "warnings": result.warnings})
    return {
        "status": "ok",
        "name": result.name,
        "path": result.path,
        "spec": result.spec,
        "warnings": result.warnings,
    }


@app.delete("/api/models/{name}")
async def delete_model_route(
    name: str,
    session_id: Optional[str] = Query(None),
    models_dir: Optional[str] = Query(None),
) -> dict:
    from redibis.services.model_service import delete_model

    base = _resolve_api_models_dir(session_id=session_id, models_dir=models_dir)
    try:
        return delete_model(name, models_dir_base=base)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/sessions/{session_id}/models/activate")
async def activate_session_model(session_id: str, body: ActivateModelBody) -> dict:
    from redibis.services.model_service import activate_model

    session = _require_session(session_id)
    try:
        base = _resolve_api_models_dir(session_id=session_id)
        path, meta = activate_model(body.name, models_dir_base=base)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    session.common_config.pii_gliner_model = path
    session.common_config.active_ner_model_name = body.name
    session.persist_to_disk()
    try:
        gs = get_config_store().load_global_settings()
        gs["pii_gliner_model"] = path
        gs["active_ner_model_name"] = body.name
        get_config_store().save_global_settings(gs)
    except Exception:
        logger.debug("global NER activation persist skipped", exc_info=True)
    return {
        "status": "activated",
        "session_id": session_id,
        "active_path": path,
        "model": meta,
        "config": session.common_config.to_dict(),
    }


@app.get("/api/agents/nodes")
def agents_nodes() -> dict:
    """Pipeline node registry for the agentic board palette (canonical + legacy)."""
    from redibis.agents.node_registry import list_nodes as list_legacy
    from redibis.agents.registry import palette_entries

    return {
        "nodes": palette_entries(),
        "legacy_nodes": [n.to_dict() for n in list_legacy()],
        "agents_enabled": _agents_enabled(),
    }


@app.get("/api/agents/registry")
def agents_registry() -> dict:
    """Canonical typed-port registry (Phase 1)."""
    from redibis.agents.registry import list_nodes

    return {
        "nodes": [n.to_dict() for n in list_nodes()],
        "agents_enabled": _agents_enabled(),
    }


@app.get("/api/agents/runtime")
def agents_runtime() -> dict:
    """Truthful capability/model summary for Agentic Ask."""
    cfg = _redibis_config()
    planner = _resolved_planner_settings()
    llm = _global_llm_defaults()
    return {
        "agents_enabled": bool(cfg.agents.enabled),
        "planner": {
            **planner,
            "when": "Natural-language planning when intent is unambiguous",
        },
        "enrichment": {
            "method": "llm" if llm.get("enabled") and llm.get("provider") else "disabled",
            "provider": llm.get("provider", ""),
            "model": llm.get("model", ""),
            "source": "global_settings" if llm.get("provider") else "not_configured",
            "when": "Only when an enrich node runs",
        },
        "codegen": {
            "method": "remote_service" if cfg.agents.codegen_service_url else "disabled",
            "provider": "gcp" if cfg.agents.codegen_service_url else "",
            "model": "",
            "source": "redibis_config",
            "when": "Only for approved policy-code generation requests",
        },
        "deterministic": [
            "profiling",
            "quality validation",
            "PII evidence and equations",
            "classification policy engine",
        ],
        "rai": {
            "enabled": bool(cfg.rai.enabled),
            "mode": cfg.rai.mode,
            "enforce": bool(cfg.rai.enforce),
            "default_residency": cfg.rai.default_residency,
        },
    }


class AgentValidateBody(BaseModel):
    pipeline: dict


@app.post("/api/agents/validate")
def agents_validate(body: AgentValidateBody) -> dict:
    """Validate port wiring and per-node config."""
    from redibis.agents.models import PipelineSpec
    from redibis.agents.registry import validate_spec

    spec = PipelineSpec.from_dict(body.pipeline)
    errors = validate_spec(spec)
    return {
        "valid": not errors,
        "errors": [e.to_dict() for e in errors],
    }


class AgentPlanBody(BaseModel):
    intent: str
    policy_pack: str = "telecom"
    database: str = ""
    jurisdiction: str = ""
    provider: str = ""
    model: str = ""


@app.post("/api/agents/plan")
def agents_plan(body: AgentPlanBody) -> dict:
    """NL intent → registry-validated pipeline (IntentPlanner)."""
    _require_agents()
    from redibis.agents.planner import IntentPlanner, PlannerContext, heuristic_plan

    cfg = _config_with_resolved_planner()
    provider_from_request = bool(body.provider)
    resolved = _resolved_planner_settings()
    if not body.provider:
        default_provider = resolved["provider"]
        default_model = body.model or resolved["model"]
        body = AgentPlanBody(
            intent=body.intent,
            policy_pack=body.policy_pack,
            database=body.database,
            jurisdiction=body.jurisdiction,
            provider=default_provider,
            model=default_model,
        )

    try:
        from redibis.agents.run_defaults import load_defaults
        run_defaults = load_defaults()
    except Exception:
        run_defaults = {}
    from redibis.agents.recipes import recipes_for_intent

    context = PlannerContext(
        policy_pack=body.policy_pack,
        database=body.database,
        jurisdiction=body.jurisdiction,
        memory_recipes=recipes_for_intent(body.intent, k=3),
        extra={"run_defaults": run_defaults},
    )
    provider = None
    fallback_reason = ""
    if body.provider:
        try:
            from redibis.enrich.providers import get_provider

            provider = get_provider(body.provider, model=body.model or "")
            result = IntentPlanner(provider=provider, redibis_config=cfg).plan(
                body.intent, context
            )
            method = "llm"
        except Exception as exc:
            fallback_reason = f"LLM planner unavailable: {type(exc).__name__}"
            result = heuristic_plan(body.intent, context)
            method = "heuristic_fallback"
    else:
        result = heuristic_plan(body.intent, context)
        method = "heuristic"
    return {
        "valid": result.valid,
        "errors": result.errors,
        "repaired": result.repaired,
        "pipeline": result.spec.to_dict(),
        "rai": result.rai_report,
        "planner": {
            "method": method,
            "provider": body.provider,
            "model": body.model or str(getattr(provider, "model", "") or ""),
            "source": "request" if provider_from_request else resolved["source"],
            "fallback_reason": fallback_reason,
        },
    }


class AgentPackBody(BaseModel):
    text: str


class AgentPackCreateBody(BaseModel):
    name: str
    text: str


@app.get("/api/agents/packs")
def agents_packs() -> dict:
    """List policy / classification packs (built-in + user) and run-defaults pack."""
    from redibis.classification.pack_store import list_packs
    names = list_packs()
    return {
        "packs": names,
        "policy": names,
        "classification": names,
        "defaults_pack": "defaults",
        "all": [{"name": n, "kind": "classification"} for n in names]
        + [{"name": "defaults", "kind": "defaults"}],
    }


class AgentDefaultsBody(BaseModel):
    text: str


@app.get("/api/agents/defaults")
def agents_defaults_get() -> dict:
    from redibis.agents.run_defaults import get_defaults_text, load_defaults
    try:
        text = get_defaults_text("defaults")
        parsed = load_defaults("defaults")
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    planner = parsed.get("planner") if isinstance(parsed.get("planner"), dict) else {}
    agentic = _global_agentic_defaults()
    planner = {
        **planner,
        "provider": agentic.get("planner_provider") or planner.get("provider") or "",
        "model": agentic.get("planner_model") or planner.get("model") or "",
    }
    parsed["planner"] = planner
    return {"name": "defaults", "text": text, "defaults": parsed}


@app.put("/api/agents/defaults")
def agents_defaults_save(body: AgentDefaultsBody) -> dict:
    _require_agents()
    from redibis.agents.run_defaults import save_defaults_text, load_defaults
    try:
        path = save_defaults_text(body.text, "defaults")
        parsed = load_defaults("defaults")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"name": "defaults", "ok": True, "path": path, "defaults": parsed}


@app.post("/api/agents/packs")
def agents_pack_create(body: AgentPackCreateBody) -> dict:
    """Create a new user pack (validated YAML)."""
    _require_agents()
    from redibis.classification.pack_store import save_pack_text
    try:
        path = save_pack_text(body.name, body.text)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"name": body.name.strip().lower(), "ok": True, "path": path}


@app.get("/api/agents/packs/{name}")
def agents_pack_get(name: str) -> dict:
    from redibis.classification.pack_store import get_pack_text
    try:
        return {"name": name, "text": get_pack_text(name)}
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.put("/api/agents/packs/{name}")
def agents_pack_save(name: str, body: AgentPackBody) -> dict:
    """Validate then save a user pack (built-ins are read-only; this writes a user copy)."""
    _require_agents()
    from redibis.classification.pack_store import save_pack_text
    try:
        path = save_pack_text(name, body.text)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"name": name, "ok": True, "path": path}


class AgentSourceBody(BaseModel):
    engine: str = "oracle"        # oracle | postgres | jdbc | hive
    jdbc: dict = {}               # {host, port, database, credential_ref}
    hive: dict = {}               # {metastore_uri, database, use_spark}
    schema_name: str = ""


def _agent_samples_root() -> Path:
    """Root directory for composer local sample workspaces."""
    cfg = _redibis_config().agents
    root = Path(cfg.sample_dir or cfg.runs_dir or "./agent_runs") / "samples"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _sanitize_sample_session_id(session_id: str) -> str:
    safe = re.sub(r"[^\w\-]", "", (session_id or "").strip())
    if not safe or len(safe) > 64:
        raise HTTPException(status_code=400, detail="invalid session_id")
    return safe


def _agent_sample_session_dir(session_id: str) -> Path:
    dest = _agent_samples_root() / _sanitize_sample_session_id(session_id)
    dest.mkdir(parents=True, exist_ok=True)
    return dest


def _agent_sample_dir() -> Path:
    """Legacy flat sample dir — kept for CLI/back-compat only; composer uses session dirs."""
    return _agent_samples_root()


def _list_sample_workspace(dest: Path, *, session_id: str) -> dict:
    entries: list[dict] = []
    for path in sorted(dest.iterdir()):
        if not path.is_file():
            continue
        if path.suffix.lower() not in (".csv", ".parquet"):
            continue
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        entries.append({"name": path.name, "table": path.stem, "size": size})
    tables = sorted({e["table"] for e in entries})
    return {
        "session_id": session_id,
        "sample_dir": str(dest),
        "files": [e["name"] for e in entries],
        "file_entries": entries,
        "tables": tables,
    }


def _unique_upload_name(dest: Path, name: str) -> str:
    """Pick a non-colliding filename when folder uploads flatten into one directory."""
    candidate = dest / name
    if not candidate.exists():
        return name
    stem = Path(name).stem
    suffix = Path(name).suffix
    n = 2
    while True:
        alt = f"{stem}_{n}{suffix}"
        if not (dest / alt).exists():
            return alt
        n += 1


def _save_upload_bytes(dest: Path, name: str, content: bytes) -> str:
    if not name or name.startswith("."):
        return ""
    if Path(name).suffix.lower() not in (".csv", ".parquet"):
        return ""
    final_name = _unique_upload_name(dest, name)
    (dest / final_name).write_bytes(content)
    return final_name


def _source_config_from_body(body: "AgentSourceBody"):
    from redibis.config import JdbcConfig, SourceConfig
    j = body.jdbc or {}
    h = body.hive or {}
    engine = (body.engine or "oracle").lower()
    return SourceConfig(
        engine=engine,
        spark_session_injected=bool(h.get("use_spark", True)),
        jdbc=JdbcConfig(
            host=str(j.get("host") or ""),
            port=int(j.get("port") or 0),
            database=str(j.get("database") or h.get("database") or j.get("service") or ""),
            credential_ref=str(j.get("credential_ref") or ""),
        ),
    )


def _source_retriever(body: "AgentSourceBody", *, spark: Any = None):
    from redibis.metadata import get_metadata_retriever
    return get_metadata_retriever(_source_config_from_body(body), spark=spark)


def _resolve_source_schema(body: "AgentSourceBody", engine: str) -> str:
    """Schema/owner for table listing — never use Oracle service name as owner."""
    if body.schema_name:
        return body.schema_name
    if engine == "oracle":
        return ""
    cfg = _source_config_from_body(body)
    return cfg.jdbc.database or ""


@app.post("/api/agents/source/test")
def agents_source_test(body: AgentSourceBody) -> dict:
    """Test an external source connection and return its tables. Never echoes secrets."""
    _require_agents()
    try:
        spark = None
        engine = (body.engine or "oracle").lower()
        if engine == "hive":
            if not (body.hive or {}).get("use_spark", True):
                return {
                    "connected": False,
                    "error": "Hive metastore URI browsing requires Spark — enable injected Spark session",
                }
            return {
                "connected": False,
                "error": "Spark session required for Hive — inject Spark in the runtime environment",
            }
        retriever = _source_retriever(body)
        schema = _resolve_source_schema(body, engine)
        tables = retriever.list_tables(schema)
        return {"connected": True, "table_count": len(tables), "tables": tables[:500]}
    except Exception as exc:  # connection / driver / Spark-missing → reported, not raised
        return {"connected": False, "error": str(exc)}


@app.get("/api/agents/source/schemas")
def agents_source_schemas(
    engine: str = "oracle",
    host: str = "",
    port: int = 0,
    database: str = "",
    credential_ref: str = "",
    use_spark: bool = True,
) -> dict:
    """List schemas / databases on an external source (credential ref only — never secrets)."""
    _require_agents()
    body = AgentSourceBody(
        engine=engine,
        jdbc={"host": host, "port": port, "database": database, "credential_ref": credential_ref},
        hive={"use_spark": use_spark, "database": database},
    )
    try:
        if engine.lower() == "hive":
            if not use_spark:
                raise HTTPException(
                    status_code=400,
                    detail="Spark session required for Hive schema browse",
                )
            raise HTTPException(
                status_code=400,
                detail="Spark session required for Hive — use POST /api/agents/source/test in runtime",
            )
        retriever = _source_retriever(body)
        if hasattr(retriever, "list_schemas"):
            return {"schemas": retriever.list_schemas()}
        return {"schemas": [database] if database else []}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/agents/source/tables")
def agents_source_tables_get(
    engine: str = "oracle",
    host: str = "",
    port: int = 0,
    database: str = "",
    credential_ref: str = "",
    schema: str = "",
) -> dict:
    """Browse tables for a schema on an external source."""
    _require_agents()
    body = AgentSourceBody(
        engine=engine,
        jdbc={"host": host, "port": port, "database": database, "credential_ref": credential_ref},
        schema_name=schema or database,
    )
    try:
        retriever = _source_retriever(body)
        tables = retriever.list_tables(body.schema_name or schema or database)
        return {"tables": tables[:1000]}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/agents/source/tables")
def agents_source_tables(body: AgentSourceBody) -> dict:
    """Browse tables for a given schema on a connected external source."""
    _require_agents()
    try:
        retriever = _source_retriever(body)
        engine = (body.engine or "oracle").lower()
        schema = body.schema_name or _resolve_source_schema(body, engine)
        return {"tables": retriever.list_tables(schema)[:1000]}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/agents/source/samples/session")
def agents_source_samples_new_session() -> dict:
    """Create an empty composer sample workspace (isolated from prior uploads)."""
    _require_agents()
    session_id = uuid4().hex[:16]
    dest = _agent_sample_session_dir(session_id)
    return _list_sample_workspace(dest, session_id=session_id)


@app.get("/api/agents/source/samples")
def agents_source_samples(session_id: str = "") -> dict:
    """List sample files for one composer workspace. Without session_id returns empty."""
    _require_agents()
    if not session_id:
        return {
            "session_id": "",
            "sample_dir": "",
            "files": [],
            "file_entries": [],
            "tables": [],
        }
    dest = _agent_sample_session_dir(session_id)
    return _list_sample_workspace(dest, session_id=_sanitize_sample_session_id(session_id))


@app.delete("/api/agents/source/samples/{filename}")
def agents_source_sample_delete(filename: str, session_id: str = "") -> dict:
    """Remove one uploaded sample file from a composer workspace."""
    _require_agents()
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")
    safe_name = Path((filename or "").replace("\\", "/").split("/")[-1]).name
    if not safe_name or safe_name.startswith("."):
        raise HTTPException(status_code=400, detail="invalid filename")
    dest = _agent_sample_session_dir(session_id)
    target = dest / safe_name
    if not target.is_file():
        raise HTTPException(status_code=404, detail=f"file not found: {safe_name!r}")
    target.unlink()
    return _list_sample_workspace(dest, session_id=_sanitize_sample_session_id(session_id))


@app.post("/api/agents/source/samples/clear")
def agents_source_samples_clear(body: dict | None = None) -> dict:
    """Delete all files in a workspace; optionally mint a fresh session_id."""
    _require_agents()
    body = body or {}
    session_id = str(body.get("session_id") or "")
    new_session = bool(body.get("new_session", True))
    if session_id:
        dest = _agent_sample_session_dir(session_id)
        if dest.is_dir():
            shutil.rmtree(dest, ignore_errors=True)
        if not new_session:
            dest.mkdir(parents=True, exist_ok=True)
            return _list_sample_workspace(dest, session_id=_sanitize_sample_session_id(session_id))
    session_id = uuid4().hex[:16]
    dest = _agent_sample_session_dir(session_id)
    return _list_sample_workspace(dest, session_id=session_id)


@app.post("/api/agents/source/upload")
async def agents_source_upload(request: Request, session_id: str = "") -> dict:
    """Upload sample CSV/Parquet files into an isolated composer workspace."""
    _require_agents()
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")
    dest = _agent_sample_session_dir(session_id)

    form = await request.form()
    uploads = list(form.getlist("files"))
    if not uploads:
        single = form.get("file")
        if single is not None:
            uploads = [single]
    if not uploads:
        raise HTTPException(status_code=400, detail="no files uploaded (use form field 'files')")

    saved: list[str] = []
    for upload in uploads:
        raw_name = (getattr(upload, "filename", None) or "sample.csv").replace("\\", "/")
        name = raw_name.split("/")[-1]
        content = await upload.read()
        final = _save_upload_bytes(dest, name, content)
        if final:
            saved.append(final)
    if not saved:
        raise HTTPException(status_code=400, detail="no supported files (.csv, .parquet) uploaded")

    out = _list_sample_workspace(dest, session_id=_sanitize_sample_session_id(session_id))
    return {"ok": True, "saved": saved, **out}


@app.get("/api/agents/recipes")
def agents_recipes() -> dict:
    """Curated starting recipes (intent + plan) for the composer."""
    from redibis.agents.recipes import list_recipes
    return {"recipes": list_recipes()}


@app.get("/api/agents/recipes/{recipe_id}")
def agents_recipe_get(recipe_id: str) -> dict:
    from redibis.agents.recipes import get_recipe
    r = get_recipe(recipe_id)
    if r is None:
        raise HTTPException(status_code=404, detail=f"recipe {recipe_id!r} not found")
    return r


class AgentProviderValidateBody(BaseModel):
    provider: str = ""
    model: str = ""


def _provider_is_cloud_hosted(name: str, provider: Any) -> bool:
    key = (name or "").lower()
    if key == "demo":
        return False
    if key in {"claude", "gemini", "openai", "openrouter"}:
        return True
    return str(getattr(provider, "residency", "") or "").lower() == "public"


def _missing_cloud_key_reason(provider_name: str) -> str:
    key = (provider_name or "provider").lower()
    env_map = {
        "gemini": "GEMINI_API_KEY or GOOGLE_API_KEY",
        "claude": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
    }
    env_hint = env_map.get(key, "the provider API key env var")
    return (
        f"Missing API key for {key!r}. Set {env_hint} on the server "
        f"(restart the webapp after export) or save a key in Settings → LLM."
    )


@app.post("/api/agents/providers/validate")
def agents_provider_validate(body: AgentProviderValidateBody) -> dict:
    """Cheap config-level validation (resolves + has key/endpoint). No live model call.

    Gates the composer's Send icon: the planner LLM must be configured before it can run.
    """
    if not body.provider:
        return {"valid": False, "reason": "no model provider selected"}
    try:
        from redibis.enrich.providers import get_provider
        provider = get_provider(body.provider, model=body.model or "")
    except Exception as exc:
        return {"valid": False, "reason": str(exc)}
    has_key = bool(getattr(provider, "api_key", None))
    has_endpoint = bool(
        getattr(provider, "endpoint_url", None) or getattr(provider, "api_base", None)
    )
    if _provider_is_cloud_hosted(body.provider, provider) and not has_key:
        return {"valid": False, "reason": _missing_cloud_key_reason(body.provider)}
    if not _provider_is_cloud_hosted(body.provider, provider) and body.provider.lower() != "demo":
        if not has_key and not has_endpoint:
            return {
                "valid": False,
                "reason": (
                    f"Local provider {body.provider!r} needs an api_base URL "
                    f"(Settings → LLM endpoint or llm_providers.json)."
                ),
            }
    return {
        "valid": True,
        "provider": body.provider,
        "model": getattr(provider, "model", body.model or ""),
        "has_credentials": has_key or has_endpoint,
    }


class AgentPreviewBody(BaseModel):
    pipeline: dict
    tables: list[str] = []
    database: str = ""
    try_on_n: int = 0


@app.post("/api/agents/preview")
def agents_preview(body: AgentPreviewBody) -> dict:
    """Pre-run scope estimate (tables/columns/likely-PII) — no scan."""
    from redibis.agents.models import PipelineSpec
    from redibis.agents.preview import estimate_pipeline_scope

    spec = PipelineSpec.from_dict(body.pipeline)
    return estimate_pipeline_scope(
        spec,
        config=_redibis_config(),
        contract_store=get_contract_store(),
        tables=body.tables or None,
        database=body.database,
        try_on_n=body.try_on_n,
    )


class PipelineCompileBody(BaseModel):
    pipeline: dict


@app.post("/api/agents/compile")
def agents_compile(body: PipelineCompileBody) -> dict:
    """Compile a pipeline graph to an export-only prompt-plan (v1)."""
    from redibis.agents.models import PipelineSpec
    from redibis.agents.prompt_compiler import compile_prompt_plan

    spec = PipelineSpec.from_dict(body.pipeline)
    plan = compile_prompt_plan(spec)
    return plan.to_dict()


@app.get("/api/classification/packs")
def classification_packs() -> dict:
    from redibis.classification.pack_store import (
        is_builtin_pack,
        is_user_pack,
        list_packs,
        user_pack_dir,
    )

    packs = []
    for name in list_packs():
        packs.append({
            "name": name,
            "builtin": is_builtin_pack(name),
            "user_copy": is_user_pack(name),
            "editable": is_user_pack(name) or not is_builtin_pack(name),
        })
    return {
        "packs": packs,
        "pack_dir": str(user_pack_dir().resolve()),
    }


class ClassificationPackBody(BaseModel):
    text: str


class EdgeRuleTestBody(BaseModel):
    column: str
    entity: Optional[str] = None
    logical_type: Optional[str] = None
    is_numeric: bool = False
    is_integer: bool = False
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    cardinality_ratio: Optional[float] = None
    null_rate: Optional[float] = None
    valid_rates: dict = {}
    checksums: dict = {}
    context_hit: Optional[bool] = None
    regions: list[str] = []
    detected: bool = True
    checksum_backed: bool = False
    overlay: list[dict] = []


class PromoteEdgeRuleBody(BaseModel):
    pack: str = "telecom"
    column: str
    from_entity: str
    to_entity: str
    note: str = ""


@app.get("/api/classification/packs/{name}")
def classification_pack_get(name: str) -> dict:
    from redibis.classification.pack_store import (
        get_pack_text,
        is_builtin_pack,
        is_user_pack,
        user_pack_dir,
    )

    try:
        text = get_pack_text(name)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "name": name,
        "text": text,
        "builtin": is_builtin_pack(name),
        "user_copy": is_user_pack(name),
        "pack_dir": str(user_pack_dir().resolve()),
    }


@app.put("/api/classification/packs/{name}")
def classification_pack_save(name: str, body: ClassificationPackBody) -> dict:
    from redibis.classification.pack_store import save_pack_text, user_pack_dir

    try:
        path = save_pack_text(name, body.text)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "name": name,
        "ok": True,
        "path": path,
        "pack_dir": str(user_pack_dir().resolve()),
    }


@app.post("/api/classification/packs/{name}/validate")
def classification_pack_validate(name: str, body: ClassificationPackBody) -> dict:
    import yaml

    from redibis.classification.policy_pack import _parse_pack
    from redibis.config import ConfigError

    try:
        raw = yaml.safe_load(body.text) or {}
        policy = _parse_pack(raw)
        return {
            "ok": True,
            "name": policy.name,
            "edge_rules": len(policy.edge_rules),
        }
    except ConfigError as exc:
        return {"ok": False, "errors": [str(exc)]}
    except Exception as exc:
        return {"ok": False, "errors": [str(exc)]}


@app.post("/api/classification/packs/{name}/test")
def classification_pack_test(name: str, body: EdgeRuleTestBody) -> dict:
    from redibis.classification.edge_rules import (
        EdgeRuleColumnContext,
        apply_edge_rules,
        merge_policy_edge_rules,
    )
    from redibis.classification.pack_store import load_pack
    from redibis.models import canonical_entity

    try:
        policy = load_pack(name)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if body.overlay:
        policy = merge_policy_edge_rules(policy, body.overlay)
    ctx = EdgeRuleColumnContext(
        column=body.column,
        entity=canonical_entity(body.entity),
        logical_type=body.logical_type,
        is_numeric=body.is_numeric,
        is_integer=body.is_integer,
        min_value=body.min_value,
        max_value=body.max_value,
        cardinality_ratio=body.cardinality_ratio,
        null_rate=body.null_rate,
        valid_rates=dict(body.valid_rates or {}),
        checksums=dict(body.checksums or {}),
        context_hit=body.context_hit,
        regions=list(body.regions or []),
        detected=body.detected,
        checksum_backed=body.checksum_backed,
    )
    result = apply_edge_rules(ctx, policy)
    return {
        "matched_rule_id": result.matched_rule_id,
        "set_entity": result.set_entity,
        "require_review": result.require_review,
        "note": result.note,
        "forbid_entity": result.forbid_entity,
    }


@app.post("/api/classification/edge-rules/promote")
def classification_edge_rule_promote(body: PromoteEdgeRuleBody) -> dict:
    """Append a promoted edge rule to the active user pack (from review approval)."""
    import yaml

    from redibis.classification.edge_rules import suggest_rule_from_correction
    from redibis.classification.pack_store import get_pack_text, save_pack_text

    rule = suggest_rule_from_correction(
        column=body.column,
        from_entity=body.from_entity,
        to_entity=body.to_entity,
        note=body.note,
    )
    try:
        text = get_pack_text(body.pack)
        raw = yaml.safe_load(text) or {}
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    rules = list(raw.get("edge_rules") or [])
    rules = [r for r in rules if r.get("id") != rule["id"]]
    rules.append(rule)
    raw["edge_rules"] = rules
    new_text = yaml.safe_dump(raw, sort_keys=False, allow_unicode=True)
    try:
        path = save_pack_text(body.pack, new_text)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "path": path, "rule": rule}


class ClassifyBody(BaseModel):
    contract: dict
    table: str
    column: Optional[str] = None
    jurisdiction: str = ""
    policy: str = "telecom"


@app.post("/api/classification/classify")
def classification_classify(body: ClassifyBody) -> dict:
    from redibis.classification import ClassificationService, JurisdictionContext, get_builtin_pack

    policy = get_builtin_pack(body.policy)
    svc = ClassificationService(policy)
    if body.jurisdiction:
        svc.set_jurisdiction(JurisdictionContext(table=body.table, jurisdiction=body.jurisdiction))
    if body.column:
        results = [svc.classify_column(body.contract, body.table, body.column)]
    else:
        results = svc.classify_contract(body.contract, body.table)
    return {
        "table": body.table,
        "results": [
            {
                "column": r.column,
                "tags": r.tag_keys(),
                "approval_role": r.approval_role,
                "escalations": r.escalations,
                "violations": r.violations,
            }
            for r in results
        ],
    }


def _agent_lineage_store() -> "LineageStore":
    from pathlib import Path
    from redibis.agents.lineage_store import LineageStore

    runs_dir = _redibis_config().agents.runs_dir or "./agent_runs"
    return LineageStore(Path(runs_dir))


class AgentBatchBody(BaseModel):
    pipeline: dict
    tables: list[str] = []
    database: str = ""
    dry_run: bool = False
    sample_paths: dict[str, str] = {}
    sample_dir: str = ""
    source: dict = {}  # {engine, jdbc:{host,port,database,credential_ref}, hive:{...}} — live connection from composer


class AgentExecuteBody(BaseModel):
    pipeline: dict
    table: str
    dry_run: bool = False
    sample_path: str = ""


class AgentAskBody(BaseModel):
    prompt: str
    source_session_id: str = ""
    tables: list[str] = []
    table: str = ""
    clarification: str = ""
    dry_run: bool = False


def _sample_paths_from_session(session_id: str) -> tuple[dict[str, str], list[str]]:
    if not session_id:
        return {}, []
    dest = _agent_sample_session_dir(session_id)
    listing = _list_sample_workspace(dest, session_id=_sanitize_sample_session_id(session_id))
    sample_paths: dict[str, str] = {}
    for entry in listing.get("file_entries") or []:
        sample_paths[str(entry["table"])] = str(dest / entry["name"])
    return sample_paths, list(listing.get("tables") or [])


def _ask_needs_clarification(profile, prompt: str, *, clarification: str) -> Optional[str]:
    from redibis.agents.orchestrator import clarification_for_profile

    return clarification_for_profile(profile, prompt, clarification=clarification)


def _source_override_from_batch(source: dict | None):
    """Build a live SourceConfig from the composer's connection (remote engines only).

    Local/folder uploads flow through ``sample_dir`` and need no override; without this
    a Hive/Oracle run would fall back to ``cfg.source`` (usually empty) and scan nothing.
    """
    if not source:
        return None
    engine = str(source.get("engine") or "").lower()
    if engine in ("", "local", "folder"):
        return None
    from redibis.config import JdbcConfig, SourceConfig
    j = source.get("jdbc") or {}
    h = source.get("hive") or {}
    return SourceConfig(
        engine=engine,
        spark_session_injected=bool(h.get("use_spark", True)),
        jdbc=JdbcConfig(
            host=str(j.get("host") or ""),
            port=int(j.get("port") or 0),
            database=str(j.get("database") or j.get("service") or h.get("database") or ""),
            credential_ref=str(j.get("credential_ref") or ""),
        ),
    )


def _agent_tool_context(cfg, *, dry_run: bool = False, sample_paths: dict | None = None, sample_dir: str = "", source_override=None) -> "ToolContext":
    from pathlib import Path
    from redibis.agents.tool_runner import ToolContext
    from redibis.services.catalog_service import CatalogService

    catalog = CatalogService.from_redibis_config(get_contract_store(), cfg)
    resolved_sample_dir = Path(sample_dir) if sample_dir else _agent_sample_dir()
    return ToolContext(
        contract_store=get_contract_store(),
        catalog_service=catalog,
        config=cfg,
        dry_run=dry_run,
        execute=True,
        sample_paths=sample_paths or {},
        sample_dir=resolved_sample_dir,
        sub_store=get_subcontract_store(),
        run_merger=get_run_merger(),
        source_config=source_override or getattr(cfg, "source", None),
    )


@app.get("/api/agents/runs")
def agents_list_runs(limit: int = 50) -> dict:
    return {"runs": _agent_lineage_store().list_runs(limit=limit)}


@app.get("/api/agents/runs/{run_id}")
def agents_run_status(run_id: str) -> dict:
    try:
        run = _agent_lineage_store().load(run_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return run.to_dict()


@app.get("/api/agents/runs/{run_id}/artifacts")
def agents_run_artifacts(run_id: str) -> dict:
    from redibis.agents.artifacts import build_run_artifact_manifest

    try:
        run = _agent_lineage_store().load(run_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    cfg = _redibis_config()
    lineage = _agent_lineage_store()
    return build_run_artifact_manifest(
        run,
        report_output_dir=Path(cfg.report.output_dir),
        scan_output_dir=_scan_output_dir(),
        storage_backend=get_backend_store(),
        runs_bucket=get_runs_bucket(),
        lineage_root=lineage.root,
        contract_store=get_contract_store(),
    )


@app.get("/api/agents/runs/{run_id}/artifacts/{artifact_id}")
def agents_run_artifact_download(run_id: str, artifact_id: str):
    from redibis.agents.artifacts import build_run_artifact_index, get_download_response

    try:
        run = _agent_lineage_store().load(run_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    cfg = _redibis_config()
    lineage = _agent_lineage_store()
    index = build_run_artifact_index(
        run,
        report_output_dir=Path(cfg.report.output_dir),
        scan_output_dir=_scan_output_dir(),
        storage_backend=get_backend_store(),
        runs_bucket=get_runs_bucket(),
        lineage_root=lineage.root,
        contract_store=get_contract_store(),
    )
    entry = index.get(artifact_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    return get_download_response(entry, storage_backend=get_backend_store())


@app.get("/api/agents/runs/{run_id}/tables")
def agents_run_tables(run_id: str) -> dict:
    from redibis.agents.table_status import table_status_rows

    try:
        run = _agent_lineage_store().load(run_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"run_id": run_id, "tables": table_status_rows(run)}


@app.get("/api/agents/runs/{run_id}/tables/{table}/summary")
def agents_table_summary(run_id: str, table: str) -> dict:
    from redibis.agents.table_status import run_summary_card

    try:
        run = _agent_lineage_store().load(run_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return run_summary_card(run, table)


@app.get("/api/agents/runs/{run_id}/queue")
def agents_review_queue(run_id: str) -> dict:
    from redibis.agents.review_queue import build_review_queue, queue_summary
    from redibis.memory.retriever import get_context_retriever

    try:
        run = _agent_lineage_store().load(run_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    retriever = get_context_retriever(
        get_contract_store().memory_config,
        memory_store=getattr(get_contract_store(), "_memory_store", None),
    )
    items = build_review_queue(run, memory_retriever=retriever)
    return {
        "run_id": run_id,
        "summary": queue_summary(items),
        "items": [i.to_dict() for i in items],
    }


class AgentHandoffBody(BaseModel):
    run_id: str
    table: str
    sample_path: str = ""


@app.post("/api/agents/handoff")
def agents_handoff(body: AgentHandoffBody) -> dict:
    """Open agent table run in manual 360° console (materialize + load_session)."""
    from redibis.services.session import loader as load_module

    materialized_id = body.run_id
    try:
        from redibis.agents.handoff import handoff_to_manual_session
        from redibis.services.session_service import session_manager

        try:
            run = _agent_lineage_store().load(body.run_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        cfg = _redibis_config()
        runs_dir = Path(cfg.report.output_dir)
        sample = Path(body.sample_path) if body.sample_path else None
        handoff_result = handoff_to_manual_session(
            run,
            body.table,
            scan_output_dir=_scan_output_dir(),
            runs_output_dir=runs_dir,
            sample_path=sample,
            session_manager=session_manager,
        )
        if isinstance(handoff_result, dict) and handoff_result.get("session_id"):
            materialized_id = handoff_result["session_id"]
    except ImportError:
        pass
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    for src in ("agent", "scan"):
        try:
            return load_module.load_session(materialized_id, src)
        except FileNotFoundError:
            continue
        except KeyError:
            raise HTTPException(status_code=400, detail=f"Unknown source {src!r}")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    raise HTTPException(
        status_code=404,
        detail=f"No session {materialized_id!r} found after agent handoff",
    )


@app.get("/api/agents/runs/{run_id}/audit")
def agents_run_audit(run_id: str) -> dict:
    from redibis.agents.dag_trace import build_audit_dag

    try:
        run = _agent_lineage_store().load(run_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return build_audit_dag(run, lineage_root=_agent_lineage_store().root)


@app.get("/api/agents/capabilities")
def agents_profiling_capabilities() -> dict:
    from redibis.agents.deep_profile import list_capabilities

    return {
        "capabilities": [
            {
                "id": c.id,
                "engine": c.engine,
                "label": c.label,
                "description": c.description,
                "cost": c.cost,
                "output": c.output,
            }
            for c in list_capabilities()
        ],
    }


class DeepProfileBody(BaseModel):
    table: str
    run_id: str
    capability_ids: list[str]
    params: dict[str, dict] = {}
    sample_path: str = ""


def _dynamic_tool_registry() -> "DynamicToolRegistry":
    from redibis.agents.dynamic_tools import DynamicToolRegistry

    cfg = _redibis_config()
    reg = DynamicToolRegistry(Path(cfg.agents.dynamic_tools_dir))
    reg.bind_approved_runners(sandbox_enabled=bool(cfg.agents.dynamic_sandbox_enabled))
    return reg


@app.get("/api/agents/codegen/status")
def agents_codegen_status() -> dict:
    """Local vs remote codegen readiness (open-core + optional hosted URL)."""
    import os

    cfg = _redibis_config()
    url = (cfg.agents.codegen_service_url or os.environ.get("REDIBIS_CODEGEN_SERVICE_URL") or "").strip()
    token = bool(os.environ.get("REDIBIS_CODEGEN_TOKEN", "").strip())
    mode = (cfg.agents.codegen_mode or "local").strip().lower()
    if mode == "remote" or (url and token):
        method = "remote" if url and token else "misconfigured_remote"
    else:
        method = "local"
    return {
        "method": method,
        "codegen_mode": mode,
        "service_url_configured": bool(url),
        "token_configured": token,
        "allow_external_codegen": bool(cfg.agents.allow_external_codegen),
        "default_target": cfg.agents.codegen_default_target,
        "local_engine": "redibis.agents.codegen_local",
        "hosted_docs": "enterprise/README.md",
    }


@app.get("/api/enterprise/status")
def enterprise_modules_status() -> dict:
    """Installed commercial / vendor modules (no secrets or private paths)."""
    from redibis.enterprise import enterprise_status_payload

    return enterprise_status_payload()


class CodegenRequestBody(BaseModel):
    intent: str
    table: str
    target_system: str = "ranger"
    residency: str = "local"
    provider: str = ""
    force_external: bool = False  # override the native-capability guard on purpose


@app.post("/api/agents/codegen/request")
def agents_codegen_request(body: CodegenRequestBody) -> dict:
    """Build egress-validated CodegenRequest (metadata only — nothing executes)."""
    from redibis.agents.codegen_egress import prepare_codegen_request

    contract = get_contract_store().get_active(body.table)
    if contract is None:
        raise HTTPException(status_code=404, detail=f"no active contract for {body.table!r}")

    cfg = _redibis_config()
    hard_block = not cfg.agents.allow_external_codegen or cfg.rai.hard_block_external_pii
    try:
        validation = prepare_codegen_request(
            intent=body.intent,
            table=body.table,
            contract=contract,
            target_system=body.target_system or cfg.agents.codegen_default_target,
            residency=body.residency,
            provider=body.provider,
            hard_block=hard_block,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not validation.allowed:
        raise HTTPException(status_code=403, detail="; ".join(validation.violations))

    return {
        "allowed": True,
        "request": validation.request.to_dict(),
        "redactions": validation.redactions,
    }


@app.post("/api/agents/codegen/submit")
def agents_codegen_submit(body: CodegenRequestBody) -> dict:
    """Submit egress-safe codegen request to commercial backend when registered.

    Guarded: external code is generated only when redibis cannot already do the job
    natively (see ``redibis.agents.capability_guard``). A native overlap short-circuits
    with ``native_capability_available`` unless the caller sets ``force_external``.
    """
    _require_agents()
    from redibis.agents.capability_guard import decide_codegen
    from redibis.agents.codegen import submit_codegen

    decision = decide_codegen(body.intent, force_external=body.force_external)
    if not decision.generate:
        return {
            "status": "native_capability_available",
            "decision": decision.to_dict(),
            "message": decision.reason,
            "hint": "Toggle the matching step(s) in the composer and Send, or resubmit with force_external=true.",
        }

    contract = get_contract_store().get_active(body.table)
    if contract is None:
        raise HTTPException(status_code=404, detail=f"no active contract for {body.table!r}")

    cfg = _redibis_config()
    hard_block = not cfg.agents.allow_external_codegen or cfg.rai.hard_block_external_pii
    try:
        return submit_codegen(
            intent=body.intent,
            table=body.table,
            contract=contract,
            target_system=body.target_system or cfg.agents.codegen_default_target,
            residency=body.residency,
            provider=body.provider,
            hard_block=hard_block,
            redibis_config=cfg,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.get("/api/agents/dynamic-tools")
def agents_list_dynamic_tools(approved_only: bool = False) -> dict:
    reg = _dynamic_tool_registry()
    tools = reg.list_tools(approved_only=approved_only)
    return {"tools": [t.to_dict() for t in tools]}


class DynamicToolRegisterBody(BaseModel):
    name: str
    description: str = ""
    version: str = "1.0.0"
    policy_scope: list[str] = []
    input_schema: dict = {}
    source_path: str = ""
    approved_by: str = ""


@app.post("/api/agents/dynamic-tools/register")
def agents_register_dynamic_tool(body: DynamicToolRegisterBody) -> dict:
    """Stage or approve a dynamic tool — never auto-executes without approved_by."""
    from redibis.agents.dynamic_tools import DynamicToolManifest

    if not body.approved_by:
        raise HTTPException(
            status_code=400,
            detail="approved_by is required — dynamic tools never auto-register",
        )

    manifest = DynamicToolManifest(
        name=body.name,
        version=body.version,
        description=body.description,
        policy_scope=body.policy_scope,
        input_schema=body.input_schema,
    )
    source = Path(body.source_path) if body.source_path else None
    reg = _dynamic_tool_registry()
    saved = reg.register(manifest, source_file=source, approved_by=body.approved_by)
    reg.bind_approved_runners()
    return saved.to_dict()


@app.post("/api/agents/deep-profile")
def agents_deep_profile(body: DeepProfileBody) -> dict:
    """Tier-2 whitelisted deep profiling — report artifact only."""
    from redibis.agents.deep_profile import run_deep_profile
    import pandas as pd

    cfg = _redibis_config()
    run_dir = Path(cfg.report.output_dir) / body.run_id
    df = None
    if body.sample_path:
        p = Path(body.sample_path)
        if p.is_file():
            df = pd.read_csv(p)
    return run_deep_profile(
        body.table,
        body.run_id,
        body.capability_ids,
        body.params,
        run_dir=run_dir,
        config=cfg,
        df=df,
    )


@app.get("/api/agents/runs/{run_id}/trace")
def agents_run_trace(run_id: str) -> dict:
    from redibis.agents.dag_trace import run_to_react_flow

    try:
        run = _agent_lineage_store().load(run_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return run_to_react_flow(run)


@app.get("/api/agents/runs/{run_id}/stream")
async def agents_run_stream(run_id: str):
    """SSE log stream for an agent run — full captured logs + step summaries."""
    from redibis.agents.run_stream import agent_run_sse_generator

    return StreamingResponse(
        agent_run_sse_generator(_agent_lineage_store(), run_id),
        media_type="text/event-stream",
    )


@app.post("/api/agents/runs/{run_id}/cancel")
def agents_run_cancel(run_id: str, reason: str = "") -> dict:
    ok = _agent_lineage_store().request_cancel(run_id, reason=reason or "cancelled via API")
    if not ok:
        raise HTTPException(status_code=400, detail="run not found or already finished")
    return {"run_id": run_id, "cancelled": True}


@app.delete("/api/agents/runs/{run_id}")
def agents_run_delete(run_id: str) -> dict:
    """Remove a single run from the board's history."""
    ok = _agent_lineage_store().delete(run_id)
    if not ok:
        raise HTTPException(status_code=404, detail="run not found")
    return {"run_id": run_id, "deleted": True}


@app.delete("/api/agents/runs")
def agents_runs_clear(keep_running: bool = True) -> dict:
    """Clear finished runs from the board (keeps in-flight runs by default)."""
    removed = _agent_lineage_store().clear(keep_running=keep_running)
    return {"removed": removed}


class AgentResumeBody(BaseModel):
    session_id: str = ""
    table: str
    approved: bool = True
    approved_by: str = ""
    note: str = ""


@app.post("/api/agents/runs/{run_id}/resume")
def agents_run_resume(run_id: str, body: AgentResumeBody) -> dict:
    """Resume a LangGraph run paused at ``interrupt()`` after steward approval."""
    _require_agents()
    from redibis.agents.executor import LangGraphExecutor
    from redibis.agents.batch_executor import BatchExecutor
    from redibis.agents.models import PipelineSpec
    from redibis.agents.run_models import RunStatus
    from redibis.agents.session import load_manifest

    store = _agent_lineage_store()
    try:
        run = store.load(run_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if run.status != RunStatus.AWAITING_HITL.value:
        raise HTTPException(
            status_code=400,
            detail=f"run {run_id!r} is not awaiting HITL (status={run.status})",
        )

    # Honor REDIBIS_CONFIG / test patches via the same loader as other agent routes.
    cfg = _redibis_config()

    spec = PipelineSpec.from_dict(run.pipeline)
    ctx = _agent_tool_context(cfg)
    resume_value = {
        "approved": body.approved,
        "approved_by": body.approved_by or "steward",
        "note": body.note,
    }

    batch_meta = run.batch_meta or {}
    is_batch = (
        batch_meta.get("executor") == "langgraph"
        and (len(run.tables or []) > 1 or batch_meta.get("paused_table"))
    )
    if is_batch:
        executor = BatchExecutor(store, tool_ctx=ctx)
        table = body.table or str(batch_meta.get("paused_table") or "")
        if not table:
            raise HTTPException(status_code=400, detail="table required for batch HITL resume")
        try:
            run = executor.resume_batch(
                spec,
                run_id,
                resume_value=resume_value,
                dry_run=False,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "run_id": run.run_id,
            "status": run.status.value if hasattr(run.status, "value") else run.status,
            "hitl_pending": run.hitl_pending,
            "session_id": run.session_id or batch_meta.get("session_id") or "",
            "batch_meta": run.batch_meta,
        }

    session_id = body.session_id or run.session_id
    if not session_id:
        raise HTTPException(
            status_code=400,
            detail="session_id required — run has no linked agentic session",
        )

    try:
        session = load_manifest(store.root, session_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    table = body.table or str(batch_meta.get("paused_table") or (run.tables or [""])[0])
    if not table:
        raise HTTPException(status_code=400, detail="table required for HITL resume")

    executor = LangGraphExecutor(store, tool_ctx=ctx, redibis_config=cfg)
    try:
        run, session = executor.resume(
            spec,
            table,
            agent_run=run,
            agent_session=session,
            resume_value=resume_value,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "run_id": run.run_id,
        "status": run.status.value if hasattr(run.status, "value") else run.status,
        "hitl_pending": run.hitl_pending,
        "session_id": session.session_id,
    }


@app.post("/api/agents/ask")
def agents_ask(body: AgentAskBody, background_tasks: BackgroundTasks) -> dict:
    """Ask surface — intent → orchestrator run (no config step)."""
    _require_agents()
    from uuid import uuid4
    from redibis.agents.orchestrator import Orchestrator, apply_intent_to_spec, detect_intent
    from redibis.agents.run_models import AgentRun, RunStatus
    from redibis.agents.batch_executor import _utc_iso

    prompt = (body.prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")

    cfg = _config_with_resolved_planner()

    if body.clarification.strip():
        plan_prompt = f"{prompt}\n\nUser clarification: {body.clarification.strip()}"
    else:
        plan_prompt = prompt

    sample_paths, session_tables = _sample_paths_from_session(body.source_session_id)
    tables = list(body.tables or session_tables or [])
    if body.table and body.table not in tables:
        tables.append(body.table)

    profile = detect_intent(prompt, cfg=cfg, tables=tables or None)
    if body.clarification.strip():
        from redibis.agents.orchestrator import apply_clarification_to_profile

        profile = apply_clarification_to_profile(profile, body.clarification)
    question = _ask_needs_clarification(profile, prompt, clarification=body.clarification)
    if question:
        return {
            "status": "clarification_required",
            "question": question,
            "profile": profile.to_dict(),
        }

    store = _agent_lineage_store()
    ctx = _agent_tool_context(cfg, dry_run=body.dry_run, sample_paths=sample_paths)
    orch = Orchestrator(lineage=store, tool_ctx=ctx, config=cfg)
    plan_result = orch.plan(plan_prompt, profile)
    resolved_planner = _resolved_planner_settings()
    planner_meta = {
        "method": plan_result.method or "heuristic",
        "purpose": "intent_planning",
        "provider": plan_result.provider,
        "model": plan_result.model,
        "source": resolved_planner["source"],
        "rai": plan_result.rai_report,
        "fallback_reason": plan_result.fallback_reason,
    }
    enrich_defaults = _global_llm_defaults()
    spec = apply_intent_to_spec(
        plan_result.spec,
        profile,
        enrich_provider=(
            enrich_defaults.get("provider", "") if enrich_defaults.get("enabled") else ""
        ),
        enrich_model=(
            enrich_defaults.get("model", "") if enrich_defaults.get("enabled") else ""
        ),
    )
    run_tables = list(profile.tables or tables or [])
    if not run_tables:
        from redibis.agents.planner import _extract_table
        import re
        slug = re.sub(r"[^\w.]+", ".", _extract_table(prompt) or "schema.table")
        run_tables = [slug]

    run_id = uuid4().hex[:16]
    routing_meta: dict[str, Any] = {}
    try:
        from redibis.enrich.capability_routing import capture_routing_snapshot

        routing_meta = capture_routing_snapshot(
            get_config_store().load_global_settings(),
            agents_cfg=cfg.agents,
        ).to_dict()
    except Exception:
        routing_meta = {}
    placeholder = AgentRun(
        run_id=run_id,
        name=spec.name or "ask",
        status=RunStatus.RUNNING,
        pipeline=spec.to_dict(),
        tables=run_tables,
        created_at=_utc_iso(),
        batch_meta={
            "source": "ask",
            "intent": profile.to_dict(),
            "prompt": prompt[:2000],
            "planner": planner_meta,
            "capability_routing": routing_meta,
        },
    )
    store.save(placeholder)

    def _execute() -> None:
        try:
            agent_run = orch.execute(spec, run_tables, dry_run=body.dry_run, run_id=run_id)
            summary = agent_run.ledger.reconcile(agent_run.steps)
            agent_run.batch_meta = {
                **dict(agent_run.batch_meta or {}),
                "source": "ask",
                "intent": profile.to_dict(),
                "ledger_summary": summary,
                "prompt": prompt[:2000],
                "planner": planner_meta,
            }
            if agent_run.status == RunStatus.RUNNING:
                agent_run.status = RunStatus.COMPLETED
            store.save(agent_run)
        except Exception as exc:
            try:
                failed = store.load(run_id)
            except FileNotFoundError:
                failed = placeholder
            failed.status = RunStatus.FAILED
            failed.error = str(exc)
            failed.finished_at = _utc_iso()
            store.save(failed)

    background_tasks.add_task(_execute)
    return {
        "status": "started",
        "run_id": run_id,
        "profile": profile.to_dict(),
        "tables": run_tables,
        "pipeline": spec.to_dict(),
        "planner": planner_meta,
    }


@app.post("/api/agents/batch")
def agents_batch_run(body: AgentBatchBody, background_tasks: BackgroundTasks) -> dict:
    """Start a batch pipeline run in the background; returns run_id immediately."""
    _require_agents()
    from uuid import uuid4
    from redibis.agents.batch_executor import BatchExecutor
    from redibis.agents.models import PipelineSpec
    from redibis.agents.tool_runner import ToolContext
    from redibis.services.catalog_service import CatalogService

    spec = PipelineSpec.from_dict(body.pipeline)
    cfg = _config_with_resolved_planner()

    catalog = CatalogService.from_redibis_config(get_contract_store(), cfg)
    store = _agent_lineage_store()
    ctx = _agent_tool_context(
        cfg,
        dry_run=body.dry_run,
        sample_paths=body.sample_paths,
        sample_dir=body.sample_dir,
        source_override=_source_override_from_batch(body.source),
    )
    executor = BatchExecutor(store, tool_ctx=ctx)
    tables = executor.resolve_tables(
        spec,
        tables=body.tables or None,
        database=body.database,
        contract_store=get_contract_store(),
    )
    if not tables:
        raise HTTPException(status_code=400, detail="no tables to process")

    run_id = uuid4().hex[:16]

    def _execute():
        executor.run(
            spec,
            tables=tables,
            database=body.database,
            dry_run=body.dry_run,
            run_id=run_id,
        )

    background_tasks.add_task(_execute)
    return {
        "status": "started",
        "run_id": run_id,
        "name": spec.name,
        "tables": tables,
    }


@app.post("/api/agents/execute")
def agents_execute_sync(body: AgentExecuteBody) -> dict:
    """Execute a pipeline synchronously for one table (Phase 5)."""
    _require_agents()
    from redibis.agents.models import PipelineSpec
    from redibis.agents.pipeline_executor import PipelineExecutor

    spec = PipelineSpec.from_dict(body.pipeline)
    cfg = _config_with_resolved_planner()

    ctx = _agent_tool_context(cfg, dry_run=body.dry_run)
    executor = PipelineExecutor(_agent_lineage_store(), tool_ctx=ctx)
    run = executor.execute(
        spec,
        body.table,
        dry_run=body.dry_run,
        sample_path=body.sample_path or None,
    )
    return run.to_dict()


@app.get("/api/agents/dashboard")
def agents_dashboard(limit: int = 50) -> dict:
    from redibis.agents.dashboard import batch_dashboard
    return batch_dashboard(_agent_lineage_store(), limit=limit)


@app.post("/api/agents/docgen")
def agents_docgen(body: PipelineCompileBody) -> dict:
    from redibis.agents.docgen import document_pipeline
    from redibis.agents.models import PipelineSpec

    spec = PipelineSpec.from_dict(body.pipeline)
    return document_pipeline(spec)


@app.get("/api/agents/docgen/registry")
def agents_docgen_registry() -> dict:
    from redibis.agents.docgen import document_registry
    return document_registry()


@app.get("/health")
@app.get("/healthz")
def health() -> dict:
    return {"status": "ok"}


@app.get("/ready")
def ready() -> JSONResponse:
    def _safe(fn):
        try:
            return bool(fn())
        except Exception:
            return False

    checks = {
        "storage": _safe(
            lambda: call_with_timeout(backend_ping, _READY_TIMEOUT_SEC)
        ),
        "config": _safe(
            lambda: call_with_timeout(
                lambda: load_global_settings() is not None,
                _READY_TIMEOUT_SEC,
            )
        ),
        "memory": _safe(memory_ready),
    }
    ok = all(checks.values())
    return JSONResponse(
        {"ready": ok, "checks": checks},
        status_code=200 if ok else 503,
    )


@app.post("/admin/reset")
def admin_reset(_user=Depends(require_admin)) -> dict:
    clear_stores()
    return {"reset": True}


def _static_asset_v(filename: str) -> str:
    import hashlib
    path = _STATIC_DIR / filename
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
        return digest
    except OSError:
        return "0"


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    """Primary scan console."""
    return _TEMPLATES.TemplateResponse(
        request=request,
        name="index.html",
        context=_page_context(
            request,
            version="1.0.0",
            asset_v=_static_asset_v("app.js"),
        ),
    )


@app.get("/batch", response_class=HTMLResponse)
def batch_console(request: Request) -> HTMLResponse:
    """Backward-compatible alias for the primary scan console."""
    return _TEMPLATES.TemplateResponse(
        request=request,
        name="index.html",
        context=_page_context(
            request,
            version="1.0.0",
            asset_v=_static_asset_v("app.js"),
        ),
    )


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> HTMLResponse:
    """Canonical settings page — full operator controls + agent runtime."""
    return _TEMPLATES.TemplateResponse(
        request=request,
        name="settings.html",
        context=_page_context(
            request,
            asset_v=_static_asset_v("app.js"),
        ),
    )


@app.get("/v2", response_class=HTMLResponse)
def v2_console(request: Request) -> HTMLResponse:
    """The v2 contract console: runs review/merge, contract view, enrich, share."""
    return _TEMPLATES.TemplateResponse(
        request=request, name="v2.html", context=_page_context(request))


@app.get("/agents", response_class=HTMLResponse)
def agents_board(request: Request) -> HTMLResponse:
    """Agentic pipeline UI — Ask + Batch Processing (Composer / Dashboard / Results)."""
    from redibis.agents.copilotkit_bridge import copilotkit_available

    cfg = _redibis_config()
    return _TEMPLATES.TemplateResponse(
        request=request,
        name="agents.html",
        context=_page_context(
            request,
            agents_enabled=_agents_enabled(),
            copilotkit_enabled=bool(cfg.agents.copilotkit_enabled and copilotkit_available()),
            asset_v=_static_asset_v("agents.js"),
        ),
    )


@app.get("/users", response_class=HTMLResponse)
def users_page(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(
        request=request,
        name="users.html",
        context=_page_context(request, asset_v=_static_asset_v("app.js")),
    )


@app.get("/share/{token}", response_class=HTMLResponse)
def share_editor(request: Request, token: str) -> HTMLResponse:
    """Scoped share editor — opens the in-scope view of the active contract."""
    return _TEMPLATES.TemplateResponse(
        request=request, name="share.html",
        context=_page_context(request, token=token))


# Final Review (column-level contract approval) — REST routes
try:
    from redibis.webapp.review_routes import register_review_routes

    register_review_routes(app, lambda: get_contract_store())
except Exception:
    logger.exception(
        "review routes failed to register — /api/contracts/*/review unavailable"
    )

# Behavior Policy Runtime — catalogue, CRUD, simulate, activate (Phase 5+)
try:
    from redibis.webapp.behavior_routes import register_behavior_routes

    register_behavior_routes(app)
except Exception:
    import logging as _logging

    _logging.getLogger("redibis.webapp").exception(
        "behavior routes failed to register — /api/behavior/* unavailable"
    )

# Portable Redibis Packs (.rdbpack) — Settings Packs tab
try:
    from redibis.webapp.pack_routes import register_pack_routes

    register_pack_routes(app)
except Exception:
    import logging as _logging

    _logging.getLogger("redibis.webapp").exception(
        "rdbpack routes failed to register — /api/rdbpack/* unavailable"
    )

# Editable enrichment prompts (Settings → Prompts; pack import/export sync)
try:
    from redibis.webapp.prompt_routes import register_prompt_routes

    register_prompt_routes(app)
except Exception:
    import logging as _logging

    _logging.getLogger("redibis.webapp").exception(
        "prompt routes failed to register — /api/prompts/* unavailable"
    )

# Free-text PII scan + de-identification playground API
try:
    from redibis.webapp.pii_text_routes import register_pii_text_routes

    register_pii_text_routes(app)
except Exception:
    import logging as _logging

    _logging.getLogger("redibis.webapp").exception(
        "pii text routes failed to register — /api/pii/text/* unavailable"
    )

# Text Gateway — explorer-facing paste/scan playground
try:
    from redibis.webapp.gateway_routes import register_gateway_routes

    register_gateway_routes(app, _TEMPLATES, _static_asset_v, _page_context)
except Exception:
    import logging as _logging

    _logging.getLogger("redibis.webapp").exception(
        "gateway routes failed to register — /gateway unavailable"
    )

# PII Reporting — commercial add-on (redibis-reports) via entry point
try:
    from redibis.reporting import register_reporting

    _rpt = register_reporting(app)
    if _rpt:
        logger.info(
            "redibis-reports add-on registered: %s",
            _rpt.get("name", "ok"),
        )
    else:
        logger.info(
            "redibis-reports not installed — /reports shows install hint"
        )

        @app.get("/reports", response_class=HTMLResponse)
        def reports_page(request: Request) -> HTMLResponse:
            """Upsell page when the commercial reporting add-on is absent."""
            return _TEMPLATES.TemplateResponse(
                request=request,
                name="reports_missing.html",
                context=_page_context(
                    request,
                    asset_v=_static_asset_v("app.js"),
                ),
            )
except Exception:
    logger.exception(
        "reporting add-on failed to register — /api/reports/* unavailable"
    )

    @app.get("/reports", response_class=HTMLResponse)
    def reports_page_fallback(request: Request) -> HTMLResponse:
        return _TEMPLATES.TemplateResponse(
            request=request,
            name="reports_missing.html",
            context=_page_context(
                request,
                asset_v=_static_asset_v("app.js"),
            ),
        )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("redibis.webapp.backend:app", host="127.0.0.1", port=8000, reload=True)


# Optional CopilotKit / AG-UI bridge (no-op when disabled or extra not installed).
try:
    from redibis.agents.copilotkit_bridge import mount_copilotkit_routes

    mount_copilotkit_routes(
        app,
        _redibis_config(),
        config_loader=_config_with_resolved_planner,
    )
except Exception:
    logger.exception(
        "CopilotKit routes failed to register — /api/copilotkit/* unavailable"
    )
