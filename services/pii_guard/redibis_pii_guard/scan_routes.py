"""Scan-side HTTP routes (split-ready — no mask imports)."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from redibis_pii_guard.auth import check_authorization


class ScanBody(BaseModel):
    kind: str = "span"  # span | column
    text: Optional[str] = None
    records: Optional[Any] = None
    columns: Optional[list[str]] = None
    language: str = "en"
    engines: str = "both"
    min_score: float = 0.35
    resolve: str = "priority"
    return_text: bool = True
    entities: Optional[list[str]] = None
    apply_verdicts: bool = True
    equation_mode: str = "balanced"
    max_chars: int = 50_000
    max_rows: int = 5_000
    max_cols: int = 200


def build_scan_router(get_service) -> APIRouter:
    router = APIRouter(tags=["scan"])

    @router.post("/scan")
    def scan(
        body: ScanBody,
        authorization: Optional[str] = Header(default=None),
    ) -> dict:
        check_authorization(authorization)
        svc = get_service()
        try:
            if body.kind == "column":
                result = svc.scan(
                    kind="column",
                    records=body.records,
                    columns=body.columns,
                    language=body.language,
                    engines=body.engines,
                    apply_verdicts=body.apply_verdicts,
                    equation_mode=body.equation_mode,
                    max_rows=body.max_rows,
                    max_cols=body.max_cols,
                )
            else:
                result = svc.scan(
                    kind="span",
                    text=body.text if body.text is not None else "",
                    language=body.language,
                    engines=body.engines,
                    min_score=body.min_score,
                    resolve=body.resolve,
                    return_text=body.return_text,
                    entities=body.entities,
                    max_chars=body.max_chars,
                )
        except ValueError as exc:
            msg = str(exc)
            code = 413 if "exceeds" in msg else 400
            raise HTTPException(status_code=code, detail=msg) from exc
        return {
            "kind": result.kind,
            "detections": [d.to_dict(return_text=body.return_text) for d in result.detections],
            "entity_counts": dict(result.entity_counts),
            "ruleset_id": result.ruleset_id,
            "ruleset_version": result.ruleset_version,
            "language": result.language,
            "engines_ran": list(result.engines_ran),
            "char_count": result.char_count,
            "truncated": result.truncated,
            "offset_unit": result.offset_unit,
        }

    @router.get("/ruleset")
    def ruleset(authorization: Optional[str] = Header(default=None)) -> dict:
        check_authorization(authorization)
        return get_service().ruleset()

    return router
