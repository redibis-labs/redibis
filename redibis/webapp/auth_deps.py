"""FastAPI dependencies for admin-gated operator endpoints."""

from __future__ import annotations

from fastapi import HTTPException, Request

from redibis.webapp.security import auth_enabled


def require_admin(request: Request):
    if not auth_enabled():
        return getattr(request.state, "user", None)
    user = getattr(request.state, "user", None)
    if user is not None and getattr(user, "role", None) == "admin":
        return user
    raise HTTPException(status_code=403, detail="admin credentials required")
