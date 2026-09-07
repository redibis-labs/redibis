"""Mask-side HTTP routes (split-ready — no scan scanner imports)."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from redibis_pii_guard.auth import check_authorization


class DeidentifyBody(BaseModel):
    kind: str = "span"
    text: Optional[str] = None
    records: Optional[Any] = None
    detections: Optional[list[dict[str, Any]]] = None
    policy_id: Optional[str] = None
    policy: Optional[dict[str, Any]] = None
    only_detected: bool = True
    seed: Optional[str] = None


def build_mask_router(get_service) -> APIRouter:
    router = APIRouter(tags=["mask"])

    @router.post("/deidentify")
    def deidentify(
        body: DeidentifyBody,
        authorization: Optional[str] = Header(default=None),
    ) -> dict:
        check_authorization(authorization)
        svc = get_service()
        try:
            out = svc.deidentify(
                kind=body.kind,
                text=body.text,
                records=body.records,
                detections=body.detections,
                policy_id=body.policy_id,
                policy=body.policy,
                only_detected=body.only_detected,
                seed=body.seed,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return out.to_dict()

    @router.get("/policies")
    def policies(authorization: Optional[str] = Header(default=None)) -> dict:
        check_authorization(authorization)
        return get_service().policies()

    return router
