"""Dashboard sessions, CSRF, rate limits, default-deny auth, and HTTPS helpers.

Mirrors the shape of the codegen service's cookie auth — no shared import,
because OSS must not depend on ``enterprise/``.

Mutating requests must send ``X-CSRF-Token``. Body-carried CSRF is only
supported on ``/login`` and ``/logout``, which verify in-handler.
``csrf_from_request`` is load-bearing and must stay handler-only: reading the
body inside ``BaseHTTPMiddleware`` would empty it before those handlers run.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import Optional
from urllib.parse import urlencode

from fastapi import Request, Response
from starlette.datastructures import URL
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, RedirectResponse

logger = logging.getLogger("redibis.webapp.security")

SESSION_COOKIE = "redibis_session"
CSRF_COOKIE = "redibis_csrf"
LOGIN_MESSAGE = "Invalid username or password."

PUBLIC_PATHS = frozenset({
    "/login",
    "/logout",
    "/health",
    "/healthz",
    "/favicon.ico",
})
PUBLIC_PREFIXES = ("/static/",)
SHARE_PREFIXES = ("/share/", "/api/share/")
ADMIN_ONLY_PREFIXES = ("/admin/", "/users", "/api/users")
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_GET_MUTATE_NAME = re.compile(r"reset|delete|clear|trigger|apply|merge|approve")

# POST endpoints that compute and return, and write nothing. Exempt from the
# explorer mutation denial, but NOT from authentication or CSRF. Keep this
# list short; do not add /api/pii/text/ — those routes stay admin-only.
READ_ONLY_POST_PREFIXES = ("/api/gateway/",)


def is_read_only_post(path: str) -> bool:
    return any(path.startswith(p) for p in READ_ONLY_POST_PREFIXES)


# Capability table (§4.1). Adding a future ``editor`` role is a row change here;
# middleware keeps calling ``role_can`` and does not grow new ``if role ==`` branches.
ROLE_CAPABILITIES: dict[str, frozenset[str]] = {
    "explorer": frozenset({"view", "export"}),
    "admin": frozenset({"view", "export", "mutate", "manage_users"}),
}


def role_can(role: str, capability: str) -> bool:
    return capability in ROLE_CAPABILITIES.get(role or "", frozenset())

LOGIN_LIMIT = 10
LOGIN_WINDOW_S = 300  # 5 minutes
SHARE_FAIL_LIMIT = 20
SHARE_FAIL_WINDOW_S = 300


def env_truthy(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def auth_enabled() -> bool:
    from redibis.webapp.store_accessors import resolve_auth_config

    return bool(resolve_auth_config().enabled)


def session_hours() -> float:
    from redibis.webapp.store_accessors import resolve_auth_config

    return float(resolve_auth_config().session_hours or 12)


# ── HTTPS / Secure cookie ───────────────────────────────────────────────────

def request_is_secure(request: Request) -> bool:
    forwarded = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    if forwarded:
        return forwarded.lower() == "https"
    return request.url.scheme == "https"


def cookie_secure(request: Request) -> bool:
    # Conditional on purpose: hardcoding Secure=True breaks local HTTP login —
    # the browser accepts the password, sets the cookie, then immediately
    # discards it, and the user bounces straight back to /login with no error.
    if env_truthy("REDIBIS_FORCE_SECURE_COOKIES"):
        return True
    return request_is_secure(request)


def _session_cookie_kwargs(request: Request) -> dict:
    return {
        "httponly": True,
        "samesite": "lax",
        "secure": cookie_secure(request),
        "path": "/",
    }


def _csrf_cookie_kwargs(request: Request) -> dict:
    # CSRF cookie is readable by JS (double-submit). Session stays HttpOnly.
    return {
        "httponly": False,
        "samesite": "lax",
        "secure": cookie_secure(request),
        "path": "/",
    }


# ── Client IP + sliding-window limiter ──────────────────────────────────────

def client_ip(request: Request) -> str:
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if forwarded:
        return forwarded
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


class SlidingWindowLimiter:
    def __init__(self, limit: int, window_s: float):
        self.limit = limit
        self.window_s = window_s
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self.window_s
        with self._lock:
            q = self._hits.setdefault(key, [])
            q[:] = [t for t in q if t > cutoff]
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True

    def over_limit(self, key: str) -> bool:
        """True when the next hit would be rejected. Does not record."""
        now = time.monotonic()
        cutoff = now - self.window_s
        with self._lock:
            q = self._hits.get(key, [])
            q[:] = [t for t in q if t > cutoff]
            return len(q) >= self.limit

    def record(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            self._hits.setdefault(key, []).append(now)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


_login_limiter = SlidingWindowLimiter(LOGIN_LIMIT, LOGIN_WINDOW_S)
_share_fail_limiter = SlidingWindowLimiter(SHARE_FAIL_LIMIT, SHARE_FAIL_WINDOW_S)


def reset_limiters() -> None:
    _login_limiter.reset()
    _share_fail_limiter.reset()


def login_allowed(request: Request) -> bool:
    return _login_limiter.allow(f"login:{client_ip(request)}")


def share_fail_keys(request: Request, token: str) -> tuple[str, str]:
    return (f"share:{client_ip(request)}", f"share-token:{token}")


def share_lookup_blocked(request: Request, token: str) -> bool:
    ip_key, tok_key = share_fail_keys(request, token)
    return _share_fail_limiter.over_limit(ip_key) or _share_fail_limiter.over_limit(tok_key)


def record_share_fail(request: Request, token: str) -> None:
    ip_key, tok_key = share_fail_keys(request, token)
    _share_fail_limiter.record(ip_key)
    _share_fail_limiter.record(tok_key)


# ── Cookies ─────────────────────────────────────────────────────────────────

def new_csrf_token() -> str:
    import secrets

    return secrets.token_urlsafe(32)


def set_session_cookie(response: Response, request: Request, token: str) -> None:
    max_age = int(session_hours() * 3600)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=max_age,
        **_session_cookie_kwargs(request),
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


def session_token(request: Request) -> str:
    return (request.cookies.get(SESSION_COOKIE) or "").strip()


def ensure_csrf(request: Request, response: Response) -> str:
    token = (
        getattr(request.state, "csrf_token", None)
        or (request.cookies.get(CSRF_COOKIE) or "").strip()
        or new_csrf_token()
    )
    request.state.csrf_token = token
    max_age = int(session_hours() * 3600)
    response.set_cookie(
        CSRF_COOKIE,
        token,
        max_age=max_age,
        **_csrf_cookie_kwargs(request),
    )
    return token


def csrf_ok(request: Request, submitted: str = "") -> bool:
    import secrets as _secrets

    expected = (request.cookies.get(CSRF_COOKIE) or "").strip()
    got = (submitted or "").strip()
    if not got:
        got = (request.headers.get("x-csrf-token") or "").strip()
    if not expected or not got:
        return False
    return _secrets.compare_digest(expected, got)


CSRF_HEADER_REQUIRED = "CSRF header required (X-CSRF-Token)"
CSRF_INVALID = "CSRF token missing or invalid"


def csrf_from_headers(request: Request) -> str:
    return (request.headers.get("x-csrf-token") or "").strip()


def csrf_failure_detail(request: Request) -> Optional[str]:
    """None when the CSRF header is valid; otherwise a 403 detail string."""
    submitted = csrf_from_headers(request)
    if csrf_ok(request, submitted):
        return None
    if not submitted:
        return CSRF_HEADER_REQUIRED
    return CSRF_INVALID


async def csrf_from_request(request: Request) -> str:
    """Header first, then form field. Call from handlers — not BaseHTTPMiddleware.

    LOAD-BEARING: do not move this into AuthMiddleware. Reading ``request.form()``
    inside BaseHTTPMiddleware consumes the body, so the login/logout handlers
    then see empty username/password and return 401.
    """
    got = csrf_from_headers(request)
    if got:
        return got
    content_type = (request.headers.get("content-type") or "").lower()
    if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        form = await request.form()
        return str(form.get("csrf_token") or form.get("csrf") or "")
    return ""


async def submitted_csrf(request: Request) -> str:
    """Header-only CSRF for middleware (must not read the request body)."""
    return csrf_from_headers(request)


def safe_next(value: str) -> str:
    raw = (value or "").strip().replace("\\", "/")
    if not raw:
        return "/"
    if "://" in raw:
        return "/"
    parsed = URL(raw)
    if parsed.netloc or parsed.scheme:
        return "/"
    if not raw.startswith("/"):
        return "/"
    if raw.startswith("//"):
        return "/"
    return raw


# ── Path helpers ────────────────────────────────────────────────────────────

def path_matches_prefix(path: str, prefix: str) -> bool:
    if prefix.endswith("/"):
        return path.startswith(prefix)
    return path == prefix or path.startswith(prefix + "/")


def is_public_path(path: str) -> bool:
    if path in PUBLIC_PATHS:
        return True
    return any(path.startswith(p) for p in PUBLIC_PREFIXES)


def share_token_from_path(path: str) -> Optional[str]:
    for prefix in SHARE_PREFIXES:
        if path.startswith(prefix):
            rest = path[len(prefix):]
            token = rest.split("/", 1)[0]
            return token or None
    return None


def is_share_path(path: str) -> bool:
    return any(path.startswith(p) for p in SHARE_PREFIXES)


def is_admin_only_path(path: str) -> bool:
    return any(path_matches_prefix(path, p) for p in ADMIN_ONLY_PREFIXES)


def is_api_request(request: Request) -> bool:
    path = request.url.path
    if path.startswith("/api/") or path.startswith("/admin/"):
        return True
    accept = (request.headers.get("accept") or "").lower()
    if "application/json" in accept and "text/html" not in accept:
        return True
    return False


def _unauthenticated(request: Request) -> Response:
    if is_api_request(request):
        return JSONResponse({"detail": "authentication required"}, status_code=401)
    nxt = request.url.path
    if request.url.query:
        nxt = f"{nxt}?{request.url.query}"
    loc = "/login"
    if nxt and nxt != "/login":
        loc = f"/login?{urlencode({'next': nxt})}"
    return RedirectResponse(loc, status_code=303)


def _forbidden(detail: str = "forbidden") -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=403)


def apply_security_headers(request: Request, response: Response) -> None:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault(
        "Content-Security-Policy-Report-Only",
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'",
    )
    if request_is_secure(request):
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )


# ── Middleware ──────────────────────────────────────────────────────────────

class AuthMiddleware(BaseHTTPMiddleware):
    """Default-deny: session required unless the path is explicitly public."""

    async def dispatch(self, request: Request, call_next):
        request.state.user = None
        request.state.share = None
        request.state.share_scope = None
        request.state.csrf_token = (
            (request.cookies.get(CSRF_COOKIE) or "").strip() or new_csrf_token()
        )

        if request.method == "OPTIONS":
            response = await call_next(request)
            apply_security_headers(request, response)
            return response

        if not auth_enabled():
            response = await call_next(request)
            apply_security_headers(request, response)
            return response

        path = request.url.path

        if is_public_path(path):
            # /login and /logout HTML forms carry CSRF in the body. Checking it
            # here would consume the body (BaseHTTPMiddleware); the handlers
            # verify the form field after they parse it.
            if request.method in MUTATING_METHODS and path not in ("/login", "/logout"):
                detail = csrf_failure_detail(request)
                if detail:
                    resp = _forbidden(detail)
                    apply_security_headers(request, resp)
                    return resp
            response = await call_next(request)
            ensure_csrf(request, response)
            apply_security_headers(request, response)
            return response

        if is_share_path(path):
            denied = await self._handle_share(request)
            if denied is not None:
                apply_security_headers(request, denied)
                return denied
            if request.method in MUTATING_METHODS:
                detail = csrf_failure_detail(request)
                if detail:
                    resp = _forbidden(detail)
                    apply_security_headers(request, resp)
                    return resp
            response = await call_next(request)
            ensure_csrf(request, response)
            apply_security_headers(request, response)
            return response

        from redibis.webapp.store_accessors import get_auth_store

        user = get_auth_store().get_session_user(session_token(request))
        if user is None:
            resp = _unauthenticated(request)
            apply_security_headers(request, resp)
            return resp

        request.state.user = user

        if request.method in MUTATING_METHODS:
            detail = csrf_failure_detail(request)
            if detail:
                resp = _forbidden(detail)
                apply_security_headers(request, resp)
                return resp
            if not role_can(user.role, "mutate") and not is_read_only_post(path):
                resp = _forbidden("explorer role is read-only")
                apply_security_headers(request, resp)
                return resp

        if is_admin_only_path(path) and not role_can(user.role, "manage_users"):
            resp = _forbidden("admin role required")
            apply_security_headers(request, resp)
            return resp

        response = await call_next(request)
        ensure_csrf(request, response)
        apply_security_headers(request, response)
        return response

    async def _handle_share(self, request: Request) -> Optional[Response]:
        token = share_token_from_path(request.url.path)
        if not token:
            return _forbidden("invalid share token")
        if share_lookup_blocked(request, token):
            return JSONResponse({"detail": "too many requests"}, status_code=429)
        from redibis.webapp.store_accessors import get_auth_store

        share = get_auth_store().get_share(token)
        if share is None or share.is_expired():
            record_share_fail(request, token)
            return _forbidden("invalid or expired share token")
        request.state.share = share
        request.state.share_scope = share.scope
        return None


class HttpsRedirectMiddleware(BaseHTTPMiddleware):
    """Opt-in HTTPS redirect. Default off — a proxy without X-Forwarded-Proto
    plus this middleware is an infinite redirect loop.
    """

    async def dispatch(self, request: Request, call_next):
        if env_truthy("REDIBIS_REQUIRE_HTTPS") and not request_is_secure(request):
            url = request.url.replace(scheme="https")
            return RedirectResponse(str(url), status_code=307)
        return await call_next(request)


# ── Periodic operator warnings ──────────────────────────────────────────────

_warn_stop = threading.Event()
_warn_thread: Optional[threading.Thread] = None


def _warn_loop() -> None:
    while not _warn_stop.wait(60):
        _emit_auth_warnings()


def _emit_auth_warnings() -> None:
    try:
        if not auth_enabled():
            logger.warning(
                "auth.enabled is false — the dashboard is unauthenticated. "
                "This flag is for local development only."
            )
            return
        from redibis.webapp.store_accessors import get_auth_store

        store = get_auth_store()
        if not store.has_admin():
            logger.warning(
                "auth: no admin account exists — nobody can sign in as operator"
            )
        elif store.has_legacy_admin_password():
            logger.warning(
                "DEFAULT CREDENTIALS: user 'admin' still authenticates with "
                "password 'admin'. Change it now. This warning repeats until it does."
            )
    except Exception:
        logger.debug("auth warning tick skipped", exc_info=True)


def start_auth_warnings() -> None:
    global _warn_thread
    _warn_stop.clear()
    _emit_auth_warnings()
    if _warn_thread is not None and _warn_thread.is_alive():
        return
    _warn_thread = threading.Thread(
        target=_warn_loop, name="redibis-auth-warnings", daemon=True
    )
    _warn_thread.start()


def stop_auth_warnings() -> None:
    _warn_stop.set()


def get_mutate_name_pattern() -> re.Pattern:
    return _GET_MUTATE_NAME
