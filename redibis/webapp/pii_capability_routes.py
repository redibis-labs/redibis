"""/api/pii/capabilities/* — the PII capability contract (see ``redibis.pii.capability``)."""

from __future__ import annotations

import threading
from typing import Any, Optional

from fastapi import HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

_LAST: dict[str, Any] = {}
_LOCK = threading.Lock()


def _verify() -> dict:
    from redibis.pii import capability as cap

    result = cap.verify(cap.load_contract())
    with _LOCK:
        _LAST["verification"] = result
    return result


def register_pii_capability_routes(app: Any) -> None:
    """Attach the capability-contract routes (JSON, verification, HTML/PDF document, guide)."""
    from redibis.pii import capability as cap

    @app.get("/api/pii/capabilities")
    def pii_capabilities() -> JSONResponse:
        """The contract JSON: every PII type with its examples, context, edge rules and limits."""
        return JSONResponse(cap.load_contract())

    @app.post("/api/pii/capabilities/verify")
    def pii_capabilities_verify() -> JSONResponse:
        """Run every example through the engines now; the result is kept for the report."""
        return JSONResponse(_verify())

    @app.get("/api/pii/capabilities/verification")
    def pii_capabilities_last() -> JSONResponse:
        """The last verification run on this server (404 before the first one)."""
        with _LOCK:
            last = _LAST.get("verification")
        if last is None:
            raise HTTPException(status_code=404, detail="not verified yet — POST /api/pii/capabilities/verify")
        return JSONResponse(last)

    @app.get("/api/pii/capabilities/report")
    def pii_capabilities_report(
        format: str = Query("html", pattern="^(html|pdf)$"),
        verify: str = Query("last", pattern="^(now|last|none)$"),
        download: bool = Query(False),
    ) -> Response:
        """The formal document. ``verify=now`` runs the examples first; ``last`` reuses the
        last run (running it when there is none); ``none`` renders without results."""
        verification: Optional[dict] = None
        if verify == "now":
            verification = _verify()
        elif verify == "last":
            with _LOCK:
                verification = _LAST.get("verification")
            verification = verification or _verify()
        html_text = cap.render_html(cap.load_contract(), verification)
        name = f"pii-capability-contract-{cap.load_contract().get('version', '')}"
        if format == "pdf":
            try:
                pdf = cap.render_pdf(html_text)
            except RuntimeError as exc:
                raise HTTPException(status_code=501, detail=str(exc)) from exc
            return Response(pdf, media_type="application/pdf",
                            headers={"Content-Disposition": f'attachment; filename="{name}.pdf"'})
        headers = {"Content-Disposition": f'attachment; filename="{name}.html"'} if download else {}
        return HTMLResponse(html_text, headers=headers)

    @app.get("/api/pii/capabilities/guide")
    def pii_capabilities_guide(format: str = Query("html", pattern="^(html|md)$")) -> Response:
        """How to add and tune PII detection (engines, edge rules, Text Gateway)."""
        if format == "md":
            return PlainTextResponse(cap.guide_markdown(), media_type="text/markdown; charset=utf-8")
        return HTMLResponse(cap.render_guide_html())
