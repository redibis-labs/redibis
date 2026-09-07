"""Shared HTTP helper for catalog publishers."""

from __future__ import annotations

import json
from typing import Any, Optional
from urllib.parse import urlencode


def catalog_http_request(
    method: str,
    url: str,
    *,
    json_body: Any = None,
    headers: Optional[dict[str, str]] = None,
    auth: Optional[tuple[str, str]] = None,
    timeout: float = 60.0,
) -> dict[str, Any]:
    try:
        import httpx
    except ImportError as exc:
        raise ImportError(
            "httpx is required for catalog push. Install: pip install 'redibis[catalog]'"
        ) from exc

    hdrs = dict(headers or {})
    if json_body is not None and "Content-Type" not in hdrs:
        hdrs["Content-Type"] = "application/json"

    with httpx.Client(timeout=timeout) as client:
        resp = client.request(
            method,
            url,
            headers=hdrs,
            auth=auth,
            content=json.dumps(json_body) if json_body is not None else None,
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {method} {url} ({resp.status_code}): {resp.text[:500]}")
    if not resp.content:
        return {}
    return resp.json()


def join_url(base: str, path: str, query: Optional[dict[str, str]] = None) -> str:
    base = base.rstrip("/")
    path = path if path.startswith("/") else f"/{path}"
    url = f"{base}{path}"
    if query:
        url = f"{url}?{urlencode(query)}"
    return url
