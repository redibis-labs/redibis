"""Deep Enrich HTTP routes — governed candidates + selective merge."""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

log = logging.getLogger("redibis.webapp.deep_enrich")


class PathVerdictBody(BaseModel):
    path: str
    decision: str
    value: Any = None
    chosen_source: str = "llm_synthesis"
    rationale_code: str = ""
    rationale_text: str = ""
    column: Optional[str] = None
    field: Optional[str] = None


class PathVerdictsBody(BaseModel):
    verdicts: list[PathVerdictBody] = Field(default_factory=list)


class MergeBody(BaseModel):
    verdicts: Optional[list[PathVerdictBody]] = None
    validate_contract: bool = True


def _actor(request: Request) -> str:
    user = getattr(request.state, "user", None)
    name = getattr(user, "username", None) if user is not None else None
    return str(name or "local")


def register_deep_enrich_routes(
    app,
    get_store: Callable,
    *,
    get_subcontract_store: Optional[Callable] = None,
    get_provider_for_role: Optional[Callable] = None,
    get_redibis_config: Optional[Callable] = None,
) -> None:
    router = APIRouter(tags=["deep-enrich"])

    def _svc():
        from redibis.services.deep_enrich_service import DeepEnrichService

        store = get_store()
        subs = get_subcontract_store() if get_subcontract_store else None
        cfg = get_redibis_config() if get_redibis_config else None
        return DeepEnrichService(store, subs, redibis_config=cfg)

    def _resolve_provider(
        *,
        analysis_mode: str,
        provider: str,
        model: str,
        api_key: str,
        endpoint_url: str,
        compare: bool = False,
    ):
        want = (analysis_mode or "").lower() == "assisted" or compare
        if not want:
            return None
        if get_provider_for_role is None:
            from redibis.enrich.providers import get_provider
            return get_provider(provider or "demo")
        try:
            provider_obj, _binding = get_provider_for_role(
                "contract.enrichment",
                provider=provider or "",
                model=model or "",
                api_key=api_key or None,
                endpoint_url=endpoint_url or None,
            )
            return provider_obj
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/api/deep-enrich/{table}/run")
    async def deep_enrich_run(
        table: str,
        request: Request,
        analysis_mode: str = Form("deterministic"),
        odcs_version: str = Form("v3.1.0"),
        output_dir: str = Form(""),
        provider: str = Form(""),
        model: str = Form(""),
        api_key: str = Form(""),
        endpoint_url: str = Form(""),
        system_prompt: str = Form(""),
        extra_context: str = Form(""),
        compare_modes: str = Form("false"),
        files: list[UploadFile] = File(default=[]),
    ) -> dict:
        """Run Deep Enrich; persist a separate candidate. Never upserts active."""
        uploaded: dict[str, bytes] = {}
        for f in files or []:
            raw = await f.read()
            uploaded[f.filename or f"upload-{len(uploaded)}"] = raw

        want_compare = str(compare_modes).lower() in ("1", "true", "yes")
        provider_obj = _resolve_provider(
            analysis_mode=analysis_mode,
            provider=provider,
            model=model,
            api_key=api_key,
            endpoint_url=endpoint_url,
            compare=want_compare,
        )
        out = output_dir or None
        try:
            return _svc().run(
                table,
                provider=provider_obj,
                analysis_mode=analysis_mode or "deterministic",
                odcs_version=odcs_version or "v3.1.0",
                uploaded=uploaded or None,
                system_prompt=system_prompt or "",
                extra_context=extra_context or "",
                output_dir=out,
                created_by=_actor(request),
                also_run_assisted_compare=want_compare,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404 if "no active" in str(exc).lower() else 400, detail=str(exc)) from exc
        except Exception as exc:
            log.exception("Deep Enrich failed for %r", table)
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/api/deep-enrich/{table}/runs")
    def deep_enrich_list(table: str) -> dict:
        return {"table": table, "runs": _svc().list_runs(table)}

    @router.get("/api/deep-enrich/{table}/runs/{run_id}")
    def deep_enrich_get(table: str, run_id: str) -> dict:
        try:
            return _svc().get(table, run_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/api/deep-enrich/{table}/latest")
    def deep_enrich_latest(table: str) -> dict:
        latest = _svc().latest(table)
        if latest is None:
            raise HTTPException(status_code=404, detail=f"no Deep Enrich runs for {table}")
        return latest

    @router.get("/api/deep-enrich/{table}/facets")
    def deep_enrich_facets(table: str) -> dict:
        return _svc().facets_for_steward(table)

    @router.post("/api/deep-enrich/{table}/runs/{run_id}/discard")
    def deep_enrich_discard(table: str, run_id: str) -> dict:
        try:
            return _svc().discard(table, run_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/api/deep-enrich/{table}/runs/{run_id}/verdicts")
    def deep_enrich_verdicts(
        table: str, run_id: str, body: PathVerdictsBody, request: Request,
    ) -> dict:
        try:
            return _svc().set_path_verdicts(
                table, run_id,
                [v.model_dump() for v in body.verdicts],
                actor=_actor(request),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/api/deep-enrich/{table}/runs/{run_id}/preview-merge")
    def deep_enrich_preview_merge(
        table: str, run_id: str, body: MergeBody = MergeBody(),
    ) -> dict:
        try:
            verdicts = [v.model_dump() for v in body.verdicts] if body.verdicts is not None else None
            return _svc().preview_merge(table, run_id, verdicts=verdicts)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/api/deep-enrich/{table}/runs/{run_id}/merge")
    def deep_enrich_merge(
        table: str, run_id: str, body: MergeBody, request: Request,
    ) -> dict:
        try:
            verdicts = [v.model_dump() for v in body.verdicts] if body.verdicts is not None else None
            return _svc().merge(
                table, run_id,
                actor=_actor(request),
                verdicts=verdicts,
                validate=body.validate_contract,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    app.include_router(router)
