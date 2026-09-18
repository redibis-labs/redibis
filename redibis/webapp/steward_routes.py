"""Thin REST surface for Steward Review. Writes go through StewardReviewService."""

from __future__ import annotations

from typing import Any, Callable, Optional, Union

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from redibis.services.review_service import ReviewInputError
from redibis.services.steward_review_service import StewardReviewService
from redibis.store.contract_store import ContractStore

StoreGetter = Union[ContractStore, Callable[[], ContractStore]]


class FieldVerdictBody(BaseModel):
    field: str
    decision: str
    chosen_source: str = ""
    chosen_run_id: str = ""
    value: Optional[Any] = None
    rationale_code: str = ""
    rationale_text: str = ""
    evidence_refs: Optional[list[str]] = None


class TableVerdictBody(BaseModel):
    item: str
    decision: str
    chosen_source: str = ""
    chosen_run_id: str = ""
    value: Optional[Any] = None
    rationale_code: str = ""
    rationale_text: str = ""


def _actor_from_request(request: Request) -> str:
    user = getattr(request.state, "user", None)
    if user is not None and getattr(user, "username", None):
        return str(user.username)
    return "local"


def _role_from_request(request: Request) -> str:
    user = getattr(request.state, "user", None)
    if user is not None and getattr(user, "role", None):
        return str(user.role)
    return "admin"


def register_steward_routes(app: Any, store_getter: StoreGetter) -> None:
    def _store() -> ContractStore:
        return store_getter() if callable(store_getter) else store_getter

    def _svc() -> StewardReviewService:
        store = _store()
        return StewardReviewService(store)

    def _call(fn, *a, **k):
        try:
            return fn(*a, **k)
        except ReviewInputError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/contracts/{table}/steward")
    def steward_overview(table: str) -> dict:
        return _call(_svc().overview, table)

    @app.get("/api/contracts/{table}/steward/columns/{column}")
    def steward_column(request: Request, table: str, column: str, samples: int = 0) -> dict:
        actor = _actor_from_request(request)
        role = _role_from_request(request)
        return _call(
            _svc().column, table, column,
            actor=actor, role=role, include_samples=bool(samples),
        )

    @app.post("/api/contracts/{table}/steward/columns/{column}/verdict")
    def steward_verdict(request: Request, table: str, column: str, body: FieldVerdictBody) -> dict:
        actor = _actor_from_request(request)
        payload = body.model_dump()
        payload["evidence_refs"] = payload.get("evidence_refs") or []
        return _call(_svc().decide, table, column, body.field, payload, actor=actor)

    @app.post("/api/contracts/{table}/steward/table/verdict")
    def steward_table_verdict(request: Request, table: str, body: TableVerdictBody) -> dict:
        actor = _actor_from_request(request)
        return _call(_svc().decide_table, table, body.item, body.model_dump(), actor=actor)

    @app.post("/api/contracts/{table}/steward/finalize")
    def steward_finalize(request: Request, table: str):
        actor = _actor_from_request(request)
        result = _call(_svc().finalize, table, actor=actor)
        try:
            from redibis.workspace.stores import current_stores
            current_stores().touch(table)
        except Exception:
            pass
        header = "guaranteed" if result.get("ok") and result.get("guaranteed") else "blocked"
        resp = JSONResponse(content=result)
        resp.headers["X-Redibis-Guarantee"] = header
        return resp

    @app.get("/api/contracts/{table}/steward/artifacts")
    def steward_artifacts(table: str) -> dict:
        return _call(_svc().list_artifacts, table)

    @app.get("/api/contracts/{table}/steward/artifacts/{name}")
    def steward_artifact_download(table: str, name: str):
        try:
            body, content_type, filename = _svc().get_artifact(table, name)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return Response(
            content=body,
            media_type=content_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
