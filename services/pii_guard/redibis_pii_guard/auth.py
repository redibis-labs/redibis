"""Vendor-token auth for the PII Guard service (mirrors codegen API-key mode)."""

from __future__ import annotations

import os
from typing import Optional


def auth_required() -> bool:
    return bool(
        (os.environ.get("REDIBIS_PII_GUARD_API_KEY") or "").strip()
        or (os.environ.get("REDIBIS_PII_GUARD_REQUIRE_AUTH") or "").strip().lower()
        in ("1", "true", "yes")
    )


def extract_bearer(authorization: Optional[str]) -> str:
    if not authorization:
        return ""
    parts = authorization.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return ""


def check_authorization(authorization: Optional[str]) -> None:
    """Raise ``HTTPException(401)`` when auth is configured and the bearer is bad."""
    from fastapi import HTTPException

    if not auth_required():
        return
    token = extract_bearer(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="Authorization Bearer required")
    expected = (os.environ.get("REDIBIS_PII_GUARD_API_KEY") or "").strip()
    if expected and token != expected:
        raise HTTPException(status_code=401, detail="invalid bearer token")
    if not expected and not token:
        raise HTTPException(status_code=401, detail="Authorization Bearer required")
