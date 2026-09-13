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

    @app.get("/api/rdbpack/versions")
    def rdbpack_versions(family_id: str = Query("")) -> dict:
        from redibis.pack.publish import default_pack_store

        store = default_pack_store()
        refs = store.list(family_id=family_id or None)
        active = {f"{L.id}@{L.version}" for L in _store().list_layers()}
        rows = []
        for ref in refs:
            payload = ref.to_dict()
            payload["active"] = f"{ref.id}@{ref.version}" in active
            rows.append(payload)
        return {"packs": rows, "count": len(rows)}

    @app.get("/api/rdbpack/versions/{uuid}")
    def rdbpack_version_detail(uuid: str) -> dict:
        from redibis.pack.publish import default_pack_store

        store = default_pack_store()
        ref = store.head(uuid)
        if ref is None:
            raise HTTPException(status_code=404, detail="unknown pack uuid")
        payload = ref.to_dict()
        payload["history"] = [r.to_dict() for r in store.history(ref.family_id)]
        from redibis.pii.run_store import get_run_store

        payload["runs"] = [
            r.to_dict() for r in get_run_store().runs_for_pack_uuid(ref.uuid, limit=50)
        ]
        return payload

    @app.post("/api/rdbpack/activate")
    def rdbpack_activate(body: dict) -> dict:
        """Activate a published pack UUID into the live stack (audited, separate from publish)."""
        from redibis.pack.publish import default_pack_store

        uid = str((body or {}).get("uuid") or "").strip()
        if not uid:
            raise HTTPException(status_code=400, detail="uuid required")
        published = default_pack_store()
        try:
            data = published.get(uid)
        except Exception as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        with tempfile.TemporaryDirectory(prefix="rdbpack-activate-") as td:
            path = Path(td) / f"{uid}.rdbpack"
            path.write_bytes(data)
            report = _call(
                _store().import_pack,
                path,
                dry_run=False,
                activate=bool((body or {}).get("activate_behavior")),
            )
            report["activated_uuid"] = uid
            return report

    @app.get("/api/rdbpack/versions/{uuid}/runs")
    def rdbpack_version_runs(uuid: str, limit: int = Query(100)) -> dict:
        from redibis.pii.run_store import get_run_store

        rows = get_run_store().runs_for_pack_uuid(uuid, limit=limit)
        return {"runs": [r.to_dict() for r in rows], "count": len(rows)}

    @app.get("/api/rdbpack/versions/{uuid}/diff")
    def rdbpack_version_diff(uuid: str, against: str = Query(...)) -> dict:
        from redibis.pack.diff import diff_pack_files, format_pack_diff
        from redibis.pack.loader import load_pack
        from redibis.pack.publish import default_pack_store

        store = default_pack_store()
        try:
            left_bytes = store.get(uuid)
            right_bytes = store.get(against)
        except Exception as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        with tempfile.TemporaryDirectory(prefix="rdbpack-vdiff-") as td:
            left_path = Path(td) / "left.rdbpack"
            right_path = Path(td) / "right.rdbpack"
            left_path.write_bytes(left_bytes)
            right_path.write_bytes(right_bytes)
            left = _call(load_pack, left_path)
            right = _call(load_pack, right_path)
            from redibis.pack.loader import pack_files_from_loaded

            diff = diff_pack_files(pack_files_from_loaded(left), pack_files_from_loaded(right))
        return {
            "left": uuid,
            "right": against,
            "diff": diff,
            "text": format_pack_diff(diff, left_label=uuid, right_label=against),
        }
