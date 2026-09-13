"""
REST surface for free-text PII scan + de-identification.

Mounted from backend.py via ``register_pii_text_routes(app)``.
Stateless — no contract writes, metadata-only logging.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import HTTPException, Query, Request
from pydantic import BaseModel, Field

from redibis.services.text_pii_service import (
    TextPIIService,
    TextPIIServiceError,
    text_pii_service_from_env,
)

logger = logging.getLogger("redibis.webapp.pii_text")


def role_may_see_full_provenance(request: Any) -> bool:
    """Full ScanProvenance blobs are admin-only when auth is on.

    Explorers still receive ``provenance_uuid`` on scan results. When auth is
    disabled the process is local-trusted and the full record is allowed.
    """
    from redibis.webapp.security import auth_enabled, role_can

    if not auth_enabled():
        return True
    user = getattr(getattr(request, "state", None), "user", None)
    role = getattr(user, "role", "") or ""
    return role_can(role, "mutate")


class TextScanBody(BaseModel):
    text: str
    language: str = "en"
    engines: str = "both"
    min_score: float = 0.35
    return_text: bool = True
    resolve: str = "priority"
    use_llm: bool = False
    entities: list[str] = Field(default_factory=list)
    max_chars: int = 50_000


class TextScanBatchBody(BaseModel):
    texts: list[str]
    language: str = "en"
    engines: str = "both"
    min_score: float = 0.35
    return_text: bool = True
    resolve: str = "priority"
    use_llm: bool = False
    entities: list[str] = Field(default_factory=list)
    max_chars: int = 50_000
    max_items: int = 50


class TextDeidBody(BaseModel):
    text: str
    language: str = "en"
    engines: str = "both"
    min_score: float = 0.35
    return_text: bool = True
    resolve: str = "priority"
    use_llm: bool = False
    entities: list[str] = Field(default_factory=list)
    max_chars: int = 50_000
    policy_id: Optional[str] = None
    policy: Optional[dict] = None


class SuggestPolicyBody(BaseModel):
    text: str
    language: str = "en"
    engines: str = "both"
    min_score: float = 0.35
    policy_id: str = "suggested"


class SavePolicyBody(BaseModel):
    policy: dict
    actor: str = ""


def register_pii_text_routes(app: Any, service_factory=None) -> None:
    """Attach /api/pii/text/* routes."""

    def _svc() -> TextPIIService:
        factory = service_factory or text_pii_service_from_env
        return factory()

    def _call(fn, *a, **k):
        try:
            return fn(*a, **k)
        except TextPIIServiceError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    @app.post("/api/pii/text/scan")
    async def pii_text_scan(
        request: Request,
        body: TextScanBody,
        provenance: str = Query(""),
    ) -> dict:
        svc = _svc()
        result = _call(
            svc.scan,
            body.text,
            language=body.language,
            engines=body.engines,
            min_score=body.min_score,
            return_text=body.return_text,
            resolve=body.resolve,
            use_llm=body.use_llm,
            entities=body.entities,
            max_chars=body.max_chars,
            include_provenance=(
                str(provenance).lower() == "full"
                and role_may_see_full_provenance(request)
            ),
        )
        _record_result(result, text=body.text, kind="api_scan")
        return result.to_dict(return_text=body.return_text)

    @app.post("/api/pii/text/scan-batch")
    async def pii_text_scan_batch(body: TextScanBatchBody) -> dict:
        svc = _svc()
        results = _call(
            svc.scan_batch,
            body.texts,
            max_items=body.max_items,
            language=body.language,
            engines=body.engines,
            min_score=body.min_score,
            return_text=body.return_text,
            resolve=body.resolve,
            use_llm=body.use_llm,
            entities=body.entities,
            max_chars=body.max_chars,
        )
        return {
            "results": [r.to_dict(return_text=body.return_text) for r in results],
            "count": len(results),
            "offset_unit": "unicode_codepoint",
        }

    @app.post("/api/pii/text/deidentify")
    async def pii_text_deidentify(body: TextDeidBody) -> dict:
        from redibis.pii.deid.policy import DeidPolicy

        svc = _svc()
        policy = DeidPolicy.from_dict(body.policy) if body.policy else None
        result, deid = _call(
            svc.deidentify,
            body.text,
            policy=policy,
            policy_id=body.policy_id,
            scan_kwargs={
                "language": body.language,
                "engines": body.engines,
                "min_score": body.min_score,
                "return_text": body.return_text,
                "resolve": body.resolve,
                "use_llm": body.use_llm,
                "entities": body.entities,
                "max_chars": body.max_chars,
            },
        )
        payload = result.to_dict(return_text=body.return_text)
        payload.update(deid.to_dict())
        for forbidden in ("master_key", "master_key_hex", "seed", "key", "keys"):
            payload.pop(forbidden, None)
        _record_result(result, text=body.text, kind="deid")
        return payload

    @app.post("/api/pii/text/suggest-policy")
    async def pii_text_suggest_policy(body: SuggestPolicyBody) -> dict:
        svc = _svc()
        policy = _call(
            svc.suggest_policy,
            body.text,
            language=body.language,
            engines=body.engines,
            min_score=body.min_score,
        )
        # Allow caller-chosen id
        from redibis.pii.deid.policy import DeidPolicy

        data = policy.to_dict()
        data["id"] = body.policy_id or policy.id
        return DeidPolicy.from_dict(data).to_dict()

    @app.post("/api/pii/text/policies")
    async def pii_text_save_policy(body: SavePolicyBody) -> dict:
        from redibis.pii.deid.policy import DeidPolicy

        svc = _svc()
        try:
            policy = DeidPolicy.from_dict(body.policy)
            path = svc.save_policy(policy)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except TextPIIServiceError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        logger.info("deid_policy_saved id=%s path=%s actor=%s", policy.id, path, body.actor)
        return {"ok": True, "policy": policy.to_dict(), "path": path}

    @app.get("/api/pii/text/entities")
    async def pii_text_entities(language: str = "en") -> dict:
        return _svc().entities(language=language)

    @app.get("/api/pii/text/policies")
    async def pii_text_policies() -> dict:
        return _svc().policies()

    @app.get("/api/pii/text/health")
    async def pii_text_health() -> dict:
        return _svc().health()

    @app.get("/api/pii/text/rules")
    async def pii_text_get_rules() -> dict:
        from redibis.pii.text_rules import compile_text_rules, default_text_rules

        stored = _load_stored_text_rules()
        effective = compile_text_rules(stored)
        return {
            "rules": effective.to_dict(),
            "stored": stored or {},
            "defaults": default_text_rules().to_dict(),
            "persisted_outside_pack": bool(stored),
        }

    @app.put("/api/pii/text/rules")
    async def pii_text_put_rules(body: dict) -> dict:
        from redibis.pii.text_rules import TextRuleOverlay, compile_text_rules

        overlay = TextRuleOverlay.from_dict(body.get("rules") if "rules" in body else body)
        loc = _save_stored_text_rules(overlay.to_dict())
        return {
            "status": "saved",
            "location": str(loc) if loc else "",
            "stored": overlay.to_dict(),
            "rules": compile_text_rules(overlay).to_dict(),
            "persisted_outside_pack": True,
        }

    @app.post("/api/pii/text/rules/dry-run")
    async def pii_text_rules_dry_run(body: dict) -> dict:
        from redibis.pii.text_rules import TextRuleOverlay
        from redibis.services.text_pii_service import TextPIIService

        text = str((body or {}).get("text") or "")
        draft = (body or {}).get("draft_rules") or (body or {}).get("rules") or {}
        overlay = TextRuleOverlay.from_dict(draft)
        svc = TextPIIService(
            redibis_config=_svc().config,
            text_rules_overlay=overlay,
            skip_stored_text_rules=True,
            merge_builtin_text_rules=True,
            ner_backend=getattr(_svc(), "_ner", None),
        )
        result = _call(
            svc.scan,
            text,
            language=str((body or {}).get("language") or "en"),
            engines=str((body or {}).get("engines") or "regex"),
            min_score=float((body or {}).get("min_score") or 0.35),
            include_provenance=True,
        )
        return result.to_dict()

    @app.post("/api/pii/text/rules/publish")
    async def pii_text_rules_publish(body: dict) -> dict:
        from redibis.pack.publish import PublishError, publish_text_gateway_pack

        try:
            ref = publish_text_gateway_pack(
                rules=(body or {}).get("rules") or {},
                gazetteers=(body or {}).get("gazetteers") or {},
                lexicons=(body or {}).get("lexicons") or {},
                version=str((body or {}).get("version") or "").strip(),
                description=str((body or {}).get("description") or ""),
                author=str((body or {}).get("author") or "operator"),
                tenant=str((body or {}).get("tenant") or ""),
                pack_id=str((body or {}).get("pack_id") or "text-gateway-rules"),
                parent_uuid=(body or {}).get("parent_uuid") or None,
                family_id=(body or {}).get("family_id") or None,
                eval_run_uuid=str((body or {}).get("eval_run_uuid") or ""),
                eval_gate_passed=(body or {}).get("eval_gate_passed"),
                eval_gate_summary=str((body or {}).get("eval_gate_summary") or ""),
                allow_unevaluated=bool((body or {}).get("allow_unevaluated")),
                unevaluated_reason=str((body or {}).get("unevaluated_reason") or ""),
                name=str((body or {}).get("name") or "operator"),
            )
        except PublishError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "pack": ref.to_dict(), "activated": False}

    @app.get("/api/pii/provenance/{provenance_uuid}")
    async def pii_get_provenance(request: Request, provenance_uuid: str) -> dict:
        from redibis.pii.run_store import get_run_store

        rec = get_run_store().get_provenance(provenance_uuid)
        if rec is None:
            raise HTTPException(status_code=404, detail="unknown provenance uuid")
        if not role_may_see_full_provenance(request):
            return {"provenance_uuid": rec.provenance_uuid}
        return rec.to_dict()

    @app.get("/api/pii/runs")
    async def pii_list_runs(
        provenance_uuid: str = "",
        pack_uuid: str = "",
        kind: str = "",
        limit: int = 100,
    ) -> dict:
        from redibis.pii.run_store import get_run_store

        store = get_run_store()
        if pack_uuid:
            rows = store.runs_for_pack_uuid(pack_uuid, limit=limit)
        else:
            rows = store.list_runs(
                provenance_uuid=provenance_uuid or None,
                kind=kind or None,
                limit=limit,
            )
        return {"runs": [r.to_dict() for r in rows], "count": len(rows)}

    @app.get("/api/pii/runs/{run_uuid}")
    async def pii_get_run(run_uuid: str) -> dict:
        from redibis.pii.run_store import get_run_store

        store = get_run_store()
        rec = store.get_run(run_uuid)
        if rec is None:
            raise HTTPException(status_code=404, detail="unknown run uuid")
        payload = rec.to_dict()
        if rec.provenance_uuid:
            prov = store.get_provenance(rec.provenance_uuid)
            if prov is not None:
                payload["provenance"] = prov.to_dict()
        return payload

    @app.post("/api/pii/text/rules/promote")
    async def pii_text_rules_promote(body: dict) -> dict:
        """Promote persisted global_settings rules into a new pack version."""
        stored = _load_stored_text_rules()
        if not stored:
            raise HTTPException(status_code=400, detail="no persisted pii_text_rules to promote")
        body = dict(body or {})
        body.setdefault("rules", stored)
        return await pii_text_rules_publish(body)


def _load_stored_text_rules() -> dict:
    try:
        from redibis.webapp.backend import get_config_store

        gs = get_config_store().load_global_settings()
        raw = gs.get("pii_text_rules")
        return raw if isinstance(raw, dict) else {}
    except Exception:
        try:
            from redibis.config import load_global_settings_optional

            raw = load_global_settings_optional().get("pii_text_rules")
            return raw if isinstance(raw, dict) else {}
        except Exception:
            return {}


def _save_stored_text_rules(payload: dict) -> object:
    from redibis.webapp.backend import get_config_store

    gs = get_config_store().load_global_settings()
    gs["pii_text_rules"] = payload
    return get_config_store().save_global_settings(gs)


def _record_result(result: Any, *, text: str, kind: str, actor: str = "api") -> None:
    try:
        from redibis.pii.run_store import get_run_store, record_run

        store = get_run_store()
        prov = None
        uid = getattr(result, "provenance_uuid", "") or ""
        if uid:
            prov = store.get_provenance(uid)
        record_run(
            kind=kind,
            provenance=prov,
            text=text,
            char_count=int(getattr(result, "char_count", 0) or 0),
            outcome={"entity_counts": dict(getattr(result, "entity_counts", {}) or {})},
            actor=actor,
            run_uuid=getattr(result, "run_uuid", None) or None,
            store=store,
        )
    except Exception:
        logger.debug("run registry write skipped", exc_info=True)
