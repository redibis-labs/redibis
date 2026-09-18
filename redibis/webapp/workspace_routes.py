"""REST surface for contract workspaces, paged listing, batches, promote, export."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from redibis.webapp.auth_deps import require_admin
from redibis.webapp.jobs import ScanQueueFullError, submit_scan_job
from redibis.webapp.security import role_can
from redibis.workspace.batch import BatchRunner, export_zip
from redibis.workspace.index import SORT_KEYS
from redibis.workspace.model import WorkspaceDenied, WorkspaceError, WorkspaceNotFound
from redibis.workspace.promote import promote_table
from redibis.workspace.registry import get_registry
from redibis.workspace.stores import current_stores, invalidate_stores, stores_for

log = logging.getLogger("redibis.webapp.workspaces")

DEFAULT_LIMIT = 50
HARD_CAP = 2000


class WorkspaceCreateBody(BaseModel):
    name: str = ""
    kind: str = "local"
    root: str = ""
    bucket: str = ""
    prefix: str = ""
    endpoint: str = ""
    read_only: bool = False
    notes: str = ""


class BatchCreateBody(BaseModel):
    kind: str
    tables: Optional[list[str]] = None
    selection: Optional[dict] = None
    options: Optional[dict] = None
    resume: bool = False
    batch_id: Optional[str] = None


class PromoteBody(BaseModel):
    target: str = "default"
    require_guarantee: bool = True
    force: bool = False


def _actor(request: Request) -> str:
    user = getattr(request.state, "user", None)
    if user is not None and getattr(user, "username", None):
        return str(user.username)
    return "local"


def _role(request: Request) -> str:
    user = getattr(request.state, "user", None)
    if user is not None and getattr(user, "role", None):
        return str(user.role)
    return "admin"


def _raise(exc: Exception) -> None:
    if isinstance(exc, WorkspaceNotFound):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, WorkspaceDenied):
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if isinstance(exc, WorkspaceError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise exc


def _rows_payload(rows, total, limit, offset) -> dict:
    return {
        "rows": [r.to_dict() for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def _resolve_tables(stores, body: BatchCreateBody) -> list[str]:
    if body.tables:
        return list(body.tables)
    sel = body.selection or {}
    rows, _total = stores.index.query(
        q=str(sel.get("q") or ""),
        status=str(sel.get("status") or ""),
        review=str(sel.get("review") or ""),
        limit=HARD_CAP,
        offset=0,
    )
    return [r.table for r in rows]


def register_workspace_routes(app: Any) -> None:
    @app.get("/api/workspaces")
    def list_workspaces(request: Request) -> dict:
        from redibis.workspace.stores import current_slug
        regs = get_registry().list()
        return {
            "workspaces": [r.to_dict() for r in regs],
            "active": request.query_params.get("ws") or current_slug(),
        }

    @app.post("/api/workspaces")
    def add_workspace(request: Request, body: WorkspaceCreateBody):
        require_admin(request)
        registry = get_registry()
        try:
            if (body.kind or "local").lower() == "s3":
                bucket = body.bucket or ""
                prefix = body.prefix or ""
                if body.root.startswith("s3://"):
                    from redibis.workspace.model import parse_s3_url
                    bucket, prefix = parse_s3_url(body.root)
                ref = registry.add_s3(
                    body.name, bucket, prefix,
                    endpoint=body.endpoint or None,
                    read_only=body.read_only,
                    notes=body.notes,
                )
            else:
                ref = registry.add_local(
                    body.name, body.root or body.prefix,
                    read_only=body.read_only,
                    notes=body.notes,
                )
        except Exception as exc:
            _raise(exc)
            raise
        invalidate_stores()
        return ref.to_dict()

    @app.delete("/api/workspaces/{slug}")
    def remove_workspace(request: Request, slug: str) -> dict:
        require_admin(request)
        try:
            get_registry().remove(slug)
        except Exception as exc:
            _raise(exc)
            raise
        invalidate_stores(slug)
        return {"removed": slug, "data_deleted": False}

    @app.post("/api/workspaces/{slug}/reindex")
    def reindex(request: Request, slug: str) -> dict:
        if not role_can(_role(request), "mutate"):
            raise HTTPException(status_code=403, detail="mutate role required")
        try:
            n = stores_for(slug).index.rebuild()
        except Exception as exc:
            _raise(exc)
            raise
        return {"slug": slug, "rows": n}

    @app.get("/api/workspaces/{slug}/contracts")
    def list_ws_contracts(
        slug: str,
        q: str = "",
        status: str = "",
        review: str = "",
        sort: str = "updated",
        desc: bool = True,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> dict:
        if sort not in SORT_KEYS:
            sort = "updated"
        try:
            stores = stores_for(slug)
            if not stores.index.rows():
                stores.index.rebuild()
            rows, total = stores.index.query(
                q=q, status=status, review=review, sort=sort, desc=desc,
                limit=min(max(limit, 0), HARD_CAP), offset=max(offset, 0),
            )
        except Exception as exc:
            _raise(exc)
            raise
        return _rows_payload(rows, total, limit, offset)

    @app.get("/api/workspaces/{slug}/contracts/{table}")
    def get_ws_contract(slug: str, table: str) -> dict:
        try:
            active = stores_for(slug).contract.get_active(table)
        except Exception as exc:
            _raise(exc)
            raise
        if active is None:
            raise HTTPException(status_code=404, detail=f"No active contract for '{table}'")
        return active

    @app.post("/api/workspaces/{slug}/contracts/{table}/promote")
    def promote(request: Request, slug: str, table: str, body: PromoteBody) -> dict:
        if not role_can(_role(request), "mutate"):
            raise HTTPException(status_code=403, detail="mutate role required")
        force = bool(body.force) or not bool(body.require_guarantee)
        if force:
            from redibis.webapp.security import auth_enabled
            user = getattr(request.state, "user", None)
            if auth_enabled() and getattr(user, "role", None) != "admin":
                raise HTTPException(status_code=403, detail="force promote is admin-only")
        try:
            return promote_table(
                stores_for(slug), table,
                target=body.target or "default",
                force=force,
                actor=_actor(request),
            )
        except Exception as exc:
            _raise(exc)
            raise

    @app.post("/api/workspaces/{slug}/batches")
    def create_batch(request: Request, slug: str, body: BatchCreateBody) -> dict:
        if not role_can(_role(request), "mutate"):
            raise HTTPException(status_code=403, detail="mutate role required")
        try:
            stores = stores_for(slug)
            tables = _resolve_tables(stores, body)
            runner = BatchRunner(stores)
            bid = runner.submit(
                body.kind, tables, options=body.options or {},
                actor=_actor(request),
                resume=body.resume,
                batch_id=body.batch_id,
            )
        except Exception as exc:
            _raise(exc)
            raise

        def _job():
            try:
                runner.run(bid)
            except Exception:
                log.exception("workspace batch %s failed", bid)

        try:
            submit_scan_job(_job)
        except ScanQueueFullError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        return runner.status(bid)

    @app.get("/api/workspaces/{slug}/batches")
    def list_batches(slug: str) -> dict:
        try:
            rows = BatchRunner(stores_for(slug)).list_recent()
        except Exception as exc:
            _raise(exc)
            raise
        return {"batches": rows}

    @app.get("/api/workspaces/{slug}/batches/{batch_id}")
    def batch_status(slug: str, batch_id: str) -> dict:
        try:
            return BatchRunner(stores_for(slug)).status(batch_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown batch")
        except Exception as exc:
            _raise(exc)
            raise

    @app.post("/api/workspaces/{slug}/batches/{batch_id}/cancel")
    def cancel_batch(request: Request, slug: str, batch_id: str) -> dict:
        if not role_can(_role(request), "mutate"):
            raise HTTPException(status_code=403, detail="mutate role required")
        try:
            return BatchRunner(stores_for(slug)).cancel(batch_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown batch")
        except Exception as exc:
            _raise(exc)
            raise

    @app.get("/api/workspaces/{slug}/export")
    def export_artifacts(
        request: Request,
        slug: str,
        tables: str = "",
        artifacts: str = "contract,verdicts,evidence,llm_context,corpus,graph",
    ):
        wanted_tables = [t.strip() for t in tables.split(",") if t.strip()]
        wanted_arts = [a.strip() for a in artifacts.split(",") if a.strip()]
        try:
            stores = stores_for(slug)
            if not wanted_tables:
                rows, _ = stores.index.query(limit=HARD_CAP, offset=0)
                wanted_tables = [r.table for r in rows]
            blob = export_zip(stores, wanted_tables, wanted_arts)
        except Exception as exc:
            _raise(exc)
            raise
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        filename = f"{slug}-{day}.zip"
        return Response(
            content=blob,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )


def list_contracts_page(
    *,
    q: str = "",
    status: str = "",
    review: str = "",
    sort: str = "updated",
    desc: bool = True,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    all_rows: bool = False,
) -> tuple[list[dict], dict[str, str]]:
    """Bounded listing used by GET /api/contracts. Returns (rows, headers)."""
    if all_rows:
        limit = HARD_CAP
        offset = 0
    else:
        limit = min(max(int(limit), 0) or DEFAULT_LIMIT, HARD_CAP)
        offset = max(int(offset), 0)
    stores = current_stores()
    if not stores.index.rows() and stores.contract.list_tables():
        stores.index.rebuild()
    rows, total = stores.index.query(
        q=q, status=status, review=review, sort=sort, desc=desc,
        limit=limit, offset=offset,
    )
    out = []
    for r in rows:
        out.append({
            "table": r.table,
            "version": r.version,
            "contract_uuid": r.contract_uuid,
            "name": r.name,
            "path": r.path,
            "workflows": r.workflows,
            "last_updated": r.updated,
            "columns": r.columns,
            "pii_columns": r.pii_columns,
            "review": r.review,
            "artifacts": r.artifacts,
            "status": r.status,
        })
    headers = {
        "X-Redibis-Total": str(total),
        "X-Redibis-Limit": str(limit),
        "X-Redibis-Offset": str(offset),
    }
    if total > len(out):
        headers["X-Redibis-Truncated"] = "1"
    if all_rows and total >= HARD_CAP:
        headers["Warning"] = f'299 redibis "contract list capped at {HARD_CAP}"'
    return out, headers
