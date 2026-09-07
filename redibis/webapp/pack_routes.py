"""
redibis.webapp.pack_routes
==========================
REST surface for portable Redibis Packs (``.rdbpack``) — Settings Packs tab.

Mounted from backend.py via ``register_pack_routes(app)``.
Thin adapter over ``redibis.pack.PackStackStore`` / ``export_default_pack``.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Optional

from fastapi import File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from redibis.pack import PackStackStore, RdbPackError, diff_pack_against_active, export_default_pack, load_pack
from redibis.pack.errors import PackCompatibilityError, PackLoadError, PackValidationError


def register_pack_routes(app: Any, *, store_factory=None) -> None:
    """Attach ``/api/rdbpack/*`` routes to the FastAPI app."""

    def _store() -> PackStackStore:
        factory = store_factory or PackStackStore
        return factory()

    def _call(fn, *a, **k):
        try:
            return fn(*a, **k)
        except (PackValidationError, PackCompatibilityError, PackLoadError, RdbPackError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/rdbpack/stack")
    def rdbpack_stack() -> dict:
        store = _store()
        layers = [L.to_dict() for L in store.list_layers()]
        return {"layers": layers, "count": len(layers), "stack_dir": str(store.root)}

    @app.get("/api/rdbpack/export-default")
    def rdbpack_export_default() -> FileResponse:
        tmp = Path(tempfile.mkdtemp(prefix="rdbpack-export-"))
        out = tmp / "redibis-default.rdbpack"
        _call(export_default_pack, out)
        return FileResponse(
            str(out),
            media_type="application/zip",
            filename="redibis-default.rdbpack",
        )

    @app.post("/api/rdbpack/validate")
    async def rdbpack_validate(file: UploadFile = File(...)) -> dict:
        data = await file.read()
        with tempfile.TemporaryDirectory(prefix="rdbpack-validate-") as td:
            path = Path(td) / (file.filename or "upload.rdbpack")
            path.write_bytes(data)
            pack = _call(load_pack, path)
            return {
                "ok": True,
                "identity": pack.manifest.identity,
                "pack_sha256": pack.pack_sha256,
                "mode": pack.manifest.mode,
                "contents": pack.manifest.contents.model_dump(mode="json"),
                "warnings": list(pack.warnings),
            }

    @app.post("/api/rdbpack/diff")
    async def rdbpack_diff(file: UploadFile = File(...)) -> dict:
        data = await file.read()
        with tempfile.TemporaryDirectory(prefix="rdbpack-diff-") as td:
            path = Path(td) / (file.filename or "upload.rdbpack")
            path.write_bytes(data)
            return _call(diff_pack_against_active, path, store=_store())

    @app.post("/api/rdbpack/import")
    async def rdbpack_import(
        file: UploadFile = File(...),
        dry_run: bool = Form(False),
        activate: bool = Form(False),
        mode: Optional[str] = Form(None),
    ) -> dict:
        data = await file.read()
        if not data:
            raise HTTPException(status_code=400, detail="empty upload")
        with tempfile.TemporaryDirectory(prefix="rdbpack-import-") as td:
            path = Path(td) / (file.filename or "upload.rdbpack")
            path.write_bytes(data)
            report = _call(
                _store().import_pack,
                path,
                dry_run=bool(dry_run),
                activate=bool(activate),
                mode=mode or None,
            )
            return report

    @app.delete("/api/rdbpack/stack/{identity}")
    def rdbpack_remove(identity: str) -> dict:
        # identity may be url-encoded id@version
        return _call(_store().remove, identity)

    @app.post("/api/rdbpack/remove")
    def rdbpack_remove_post(body: dict) -> dict:
        identity = str((body or {}).get("identity") or "").strip()
        if not identity:
            raise HTTPException(status_code=400, detail="identity required")
        return _call(_store().remove, identity)
