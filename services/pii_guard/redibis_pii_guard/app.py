"""FastAPI app for the standalone PII Guard service."""

from __future__ import annotations

import os
from typing import Any, Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from redibis_pii_guard.auth import check_authorization
from redibis_pii_guard.mask_routes import build_mask_router
from redibis_pii_guard.scan_routes import build_scan_router
from redibis_pii_guard.service import PiiGuardService

_SERVICE: Optional[PiiGuardService] = None


class ScanAndMaskBody(BaseModel):
    kind: str = "span"
    text: Optional[str] = None
    records: Optional[Any] = None
    policy_id: Optional[str] = None
    policy: Optional[dict[str, Any]] = None
    language: str = "en"
    engines: str = "both"
    min_score: float = 0.35
    resolve: str = "priority"
    return_text: bool = True
    apply_verdicts: bool = True
    equation_mode: str = "balanced"
    only_detected: bool = True
    seed: Optional[str] = None
    max_chars: int = 50_000


def get_service() -> PiiGuardService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = PiiGuardService()
    return _SERVICE


def create_app(*, service: Optional[PiiGuardService] = None) -> FastAPI:
    global _SERVICE
    if service is not None:
        _SERVICE = service

    app = FastAPI(
        title="Redibis PII Guard",
        version="0.1.0",
        description=(
            "Stateless scan + de-identify service. Scan and mask backends are "
            "separate modules so they can later split into pii-scan / pii-mask."
        ),
    )
    app.include_router(build_scan_router(get_service))
    app.include_router(build_mask_router(get_service))

    @app.get("/health")
    def health() -> dict:
        # Health stays unauthenticated for probes.
        return get_service().health()

    @app.post("/scan-and-mask")
    def scan_and_mask(
        payload: ScanAndMaskBody,
        authorization: Optional[str] = Header(default=None),
    ) -> dict:
        check_authorization(authorization)
        svc = get_service()
        scan_kwargs: dict[str, Any] = {
            "language": payload.language,
            "engines": payload.engines,
        }
        if payload.kind == "span":
            scan_kwargs.update(
                min_score=payload.min_score,
                resolve=payload.resolve,
                return_text=payload.return_text,
                max_chars=payload.max_chars,
            )
        else:
            scan_kwargs.update(
                apply_verdicts=payload.apply_verdicts,
                equation_mode=payload.equation_mode,
            )
        try:
            result, deid = svc.scan_and_mask(
                kind=payload.kind,
                text=payload.text,
                records=payload.records,
                policy_id=payload.policy_id,
                policy=payload.policy,
                scan_kwargs=scan_kwargs,
                only_detected=payload.only_detected,
                seed=payload.seed,
            )
        except ValueError as exc:
            msg = str(exc)
            code = 413 if "exceeds" in msg else 400
            raise HTTPException(status_code=code, detail=msg) from exc
        return {
            "scan": {
                "kind": result.kind,
                "detections": [
                    d.to_dict(return_text=payload.return_text) for d in result.detections
                ],
                "entity_counts": dict(result.entity_counts),
                "ruleset_id": result.ruleset_id,
                "ruleset_version": result.ruleset_version,
                "language": result.language,
                "offset_unit": result.offset_unit,
            },
            "deidentify": deid.to_dict(),
        }

    return app


app = create_app()


def main() -> None:
    import uvicorn

    port = int(os.environ.get("PORT", "8090"))
    uvicorn.run(
        "redibis_pii_guard.app:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=port,
        reload=False,
    )


if __name__ == "__main__":
    main()
