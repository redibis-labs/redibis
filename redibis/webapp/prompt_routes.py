"""
redibis.webapp.prompt_routes
============================
REST for editable enrichment prompt markdown (Settings → Prompts).

Bidirectional with packs:
  - GET/PUT here edits ``configs/prompts/enrich/*.md``
  - pack export reads the same store
  - pack import writes into the same store
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import HTTPException, Query
from pydantic import BaseModel, Field

from redibis.enrich.prompt_store import EnrichPromptStore, get_enrich_prompt_store


class PromptBody(BaseModel):
    content: str = Field(..., min_length=1)


class PromptBulkBody(BaseModel):
    files: dict[str, str] = Field(default_factory=dict)


def register_prompt_routes(app: Any, *, store_factory=None) -> None:
    def _store() -> EnrichPromptStore:
        factory = store_factory or get_enrich_prompt_store
        return factory()

    @app.get("/api/prompts/enrich")
    def prompts_enrich_list() -> dict:
        return _store().snapshot()

    @app.get("/api/prompts/enrich/{name:path}")
    def prompts_enrich_get(name: str) -> dict:
        try:
            content = _store().read(name)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"name": name, "content": content, "chars": len(content)}

    @app.put("/api/prompts/enrich/{name:path}")
    def prompts_enrich_put(name: str, body: PromptBody) -> dict:
        try:
            meta = _store().write(name, body.content)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        snap = _store().snapshot()
        return {"status": "saved", "file": meta, "composed_chars": snap["composed_chars"]}

    @app.put("/api/prompts/enrich")
    def prompts_enrich_bulk(body: PromptBulkBody) -> dict:
        store = _store()
        saved = []
        try:
            for name, content in (body.files or {}).items():
                saved.append(store.write(name, content))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        snap = store.snapshot()
        return {
            "status": "saved",
            "saved": saved,
            "composed_chars": snap["composed_chars"],
        }

    @app.post("/api/prompts/enrich/reset")
    def prompts_enrich_reset(name: Optional[str] = Query(None)) -> dict:
        try:
            written = _store().reset_to_builtin(name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"status": "reset", "written": written, "snapshot": _store().snapshot()}

    @app.delete("/api/prompts/enrich/{name:path}")
    def prompts_enrich_delete(name: str) -> dict:
        try:
            ok = _store().delete(name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not ok:
            raise HTTPException(status_code=404, detail=f"prompt not found: {name}")
        return {"status": "deleted", "name": name, "snapshot": _store().snapshot()}
