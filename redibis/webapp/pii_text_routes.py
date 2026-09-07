"""
REST surface for free-text PII scan + de-identification.

Mounted from backend.py via ``register_pii_text_routes(app)``.
Stateless — no contract writes, metadata-only logging.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import HTTPException
from pydantic import BaseModel, Field

from redibis.services.text_pii_service import (
    TextPIIService,
    TextPIIServiceError,
    text_pii_service_from_env,
)

logger = logging.getLogger("redibis.webapp.pii_text")


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
    async def pii_text_scan(body: TextScanBody) -> dict:
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
        )
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
