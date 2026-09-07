"""Dashboard authentication: default-deny, roles, sessions, CSRF, backends."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from redibis.config import ConfigError, RedibisConfig
from redibis.store.auth_backend import DbAuthBackend, JsonAuthBackend, StorageAuthBackend
from redibis.store.storage_backend import LocalBackend
from redibis.store.auth_store import (
    MIN_PASSWORD_CHARS,
    AuthSession,
    AuthStore,
    User,
    apply_scoped_edits,
    hash_password,
    map_stored_role,
    verify_password,
)
from redibis.webapp.security import (
    CSRF_HEADER_REQUIRED,
    LOGIN_MESSAGE,
    PUBLIC_PATHS,
    PUBLIC_PREFIXES,
    SHARE_PREFIXES,
    get_mutate_name_pattern,
    is_public_path,
    reset_limiters,
    safe_next,
)
from redibis.webapp.store_accessors import clear_stores, resolve_auth_config

ADMIN_PW = "admin-password"
EXPLORER_PW = "explorer-pass"
CSRF_COOKIE = "redibis_csrf"
SESSION_COOKIE = "redibis_session"


@pytest.fixture(autouse=True)
def _auth_env(tmp_path, monkeypatch):
    monkeypatch.setenv("REDIBIS_AUTH_ENABLED", "1")
    monkeypatch.setenv("REDIBIS_AUTH_BACKEND", "json")
    monkeypatch.setenv("REDIBIS_AUTH_PATH", str(tmp_path / "auth" / "users.json"))
    monkeypatch.setenv("REDIBIS_ADMIN_USER", "admin")
    monkeypatch.setenv("REDIBIS_ADMIN_PASSWORD", ADMIN_PW)
    monkeypatch.setenv("USE_LOCAL_STORAGE", "true")
    monkeypatch.setenv("LOCAL_STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.delenv("REDIBIS_FORCE_SECURE_COOKIES", raising=False)
    monkeypatch.delenv("REDIBIS_REQUIRE_HTTPS", raising=False)
    monkeypatch.delenv("REDIBIS_AUTH_DB", raising=False)
    clear_stores()
    reset_limiters()
    yield
    clear_stores()
    reset_limiters()


@pytest.fixture(params=["json", "sqlite"])
def auth_store(request, tmp_path):
    if request.param == "json":
        backend = JsonAuthBackend(tmp_path / "users.json")
    else:
        backend = DbAuthBackend(f"sqlite:///{tmp_path / 'auth.db'}")
    return AuthStore(backend)


@pytest.fixture
def client(_auth_env):
    from redibis.webapp.backend import app

    with TestClient(app) as c:
        yield c


def _csrf(client: TestClient) -> str:
    client.get("/login")
    return client.cookies.get(CSRF_COOKIE) or ""


def _login(client: TestClient, username: str, password: str, **headers):
    token = _csrf(client)
    return client.post(
        "/login",
        json={"username": username, "password": password},
        headers={"X-CSRF-Token": token, "Accept": "application/json", **headers},
    )


def _concrete_path(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", path)


# ── Default-deny ────────────────────────────────────────────────────────────

def test_unauthenticated_routes_are_denied_or_public(client):
    from fastapi.routing import APIRoute
    from redibis.webapp.backend import app
    from starlette.routing import Mount

    # Do not follow 303→/login; that would make every HTML path look public.
    client.follow_redirects = False
    for route in app.routes:
        if isinstance(route, Mount):
            mount_path = (route.path or "/").rstrip("/") + "/"
            if any(mount_path.startswith(p) or p.startswith(mount_path) for p in PUBLIC_PREFIXES):
                continue
            r = client.get(route.path or "/")
            if not is_public_path(route.path or "/") and not any(
                (route.path or "/").startswith(p) for p in SHARE_PREFIXES
            ):
                assert r.status_code in (401, 303, 404, 405), route.path
            continue
        if not isinstance(route, APIRoute):
            continue
        path = _concrete_path(route.path)
        if is_public_path(path) or any(path.startswith(p) for p in SHARE_PREFIXES):
            continue
        methods = set(route.methods or ()) - {"HEAD", "OPTIONS"}
        for method in methods:
            r = client.request(method, path)
            assert r.status_code in (401, 303), f"{method} {path} -> {r.status_code}"


def test_no_get_handler_mutates_by_name():
    from fastapi.routing import APIRoute
    from redibis.webapp.backend import app

    pat = get_mutate_name_pattern()
    offenders = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if "GET" not in (route.methods or ()):
            continue
        name = getattr(route.endpoint, "__name__", "") or ""
        if pat.search(name):
            offenders.append(f"{route.path} -> {name}")
    assert offenders == []


def test_health_is_liveness_only(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    blob = str(body).lower()
    assert "version" not in blob
    assert "config" not in blob
    assert "store" not in blob
    assert "bucket" not in blob
    assert body == {"status": "ok"}
    assert client.get("/healthz").status_code == 200


# ── Roles ───────────────────────────────────────────────────────────────────

def test_explorer_get_read_ok_mutating_forbidden(client):
    from redibis.webapp.store_accessors import get_auth_store

    get_auth_store().create_user("reader", EXPLORER_PW, role="explorer")
    assert _login(client, "reader", EXPLORER_PW).status_code == 200
    r = client.get("/api/contracts")
    assert r.status_code == 200
    r = client.post("/api/sessions", json={})
    assert r.status_code == 403
    r = client.put("/api/settings/global", json={})
    assert r.status_code == 403
    r = client.delete("/api/users/reader")
    assert r.status_code == 403
    r = client.get("/api/users")
    assert r.status_code == 403
    r = client.get("/users")
    assert r.status_code == 403


def test_admin_not_forbidden_on_mutating(client):
    assert _login(client, "admin", ADMIN_PW).status_code == 200
    csrf = client.cookies.get(CSRF_COOKIE) or ""
    r = client.post("/admin/reset", headers={"X-CSRF-Token": csrf})
    assert r.status_code != 403
    r = client.get("/api/users")
    assert r.status_code == 200


def test_unknown_role_maps_to_explorer_never_admin():
    assert map_stored_role("superuser", username="x") == "explorer"
    u = User.from_dict({
        "username": "x",
        "password_hash": "abc",
        "role": "root",
    })
    assert u.role == "explorer"


def test_editor_maps_to_admin_and_logs(caplog):
    caplog.set_level(logging.WARNING)
    role = map_stored_role("editor", username="pat")
    assert role == "admin"
    assert "pat" in caplog.text
    assert "editor" in caplog.text


def test_capability_table_is_the_policy():
    from redibis.webapp.security import ROLE_CAPABILITIES, role_can

    assert role_can("explorer", "view")
    assert role_can("explorer", "export")
    assert not role_can("explorer", "mutate")
    assert not role_can("explorer", "manage_users")
    assert role_can("admin", "mutate")
    assert role_can("admin", "manage_users")
    assert set(ROLE_CAPABILITIES) == {"admin", "explorer"}


# ── Passwords ───────────────────────────────────────────────────────────────

def test_legacy_sha256_authenticates_and_rehashes(auth_store):
    legacy = hashlib.sha256("legacy-secret-1".encode()).hexdigest()
    auth_store._backend.save_user(User(
        username="legacy", password_hash=legacy, role="explorer",
    ))
    user = auth_store.authenticate("legacy", "legacy-secret-1")
    assert user is not None
    stored = auth_store.get_user("legacy").password_hash
    assert stored.startswith("pbkdf2_sha256$")
    assert auth_store.authenticate("legacy", "legacy-secret-1") is not None


def test_new_hashes_differ_for_same_password():
    assert hash_password("same-password-1") != hash_password("same-password-1")


def test_wrong_password_fails_both_formats(auth_store):
    auth_store.create_user("n", "correct-pass1", role="explorer")
    assert auth_store.authenticate("n", "wrong-password") is None
    legacy = hashlib.sha256("legacy-secret-1".encode()).hexdigest()
    auth_store._backend.save_user(User(
        username="old", password_hash=legacy, role="explorer",
    ))
    assert auth_store.authenticate("old", "nope-nope-nope") is None
    ok, _ = verify_password("x", legacy)
    assert ok is False


def test_short_password_rejected_on_create(auth_store):
    with pytest.raises(ValueError, match="at least"):
        auth_store.create_user("shorty", "12345678901", role="explorer")
    assert MIN_PASSWORD_CHARS == 12


# ── Sessions / CSRF / rate limit ────────────────────────────────────────────

def test_login_sets_httponly_session_cookie(client):
    r = _login(client, "admin", ADMIN_PW)
    assert r.status_code == 200
    header = _set_cookie_header(r)
    assert SESSION_COOKIE in header
    assert "httponly" in header
    assert "samesite=lax" in header


def test_html_form_login_succeeds_without_csrf_header(client):
    client.follow_redirects = False
    assert client.get("/login").status_code == 200
    token = client.cookies.get(CSRF_COOKIE) or ""
    r = client.post(
        "/login",
        data={
            "username": "admin",
            "password": ADMIN_PW,
            "csrf_token": token,
            "next": "/",
        },
    )
    assert r.status_code == 303
    assert client.cookies.get(SESSION_COOKIE)


def test_html_form_login_without_csrf_field_is_403(client):
    client.get("/login")
    r = client.post(
        "/login",
        data={"username": "admin", "password": ADMIN_PW, "next": "/"},
    )
    assert r.status_code == 403


def test_logout_revokes_server_side(client):
    assert _login(client, "admin", ADMIN_PW).status_code == 200
    csrf = client.cookies.get(CSRF_COOKIE)
    r = client.post("/logout", headers={"X-CSRF-Token": csrf, "Accept": "application/json"})
    assert r.status_code == 200
    r = client.get("/api/contracts")
    assert r.status_code in (401, 303)


def test_expired_session_is_401(client, tmp_path):
    from redibis.webapp.store_accessors import get_auth_store

    store = get_auth_store()
    token = store.create_session("admin", hours=-1)
    client.cookies.set(SESSION_COOKIE, token)
    r = client.get("/api/contracts")
    assert r.status_code in (401, 303)


def test_mutating_without_csrf_is_403(client):
    assert _login(client, "admin", ADMIN_PW).status_code == 200
    # Drop CSRF header; TestClient still sends the session cookie.
    r = client.post(
        "/admin/reset",
        headers={"X-CSRF-Token": "", "Accept": "application/json"},
    )
    # empty header → csrf_ok false
    assert r.status_code == 403


def test_eleventh_login_is_429(client):
    token = _csrf(client)
    last = None
    for _ in range(11):
        last = client.post(
            "/login",
            json={"username": "admin", "password": "wrong-password"},
            headers={"X-CSRF-Token": token, "Accept": "application/json"},
        )
    assert last.status_code == 429


def test_unknown_user_and_wrong_password_same_message(client):
    token = _csrf(client)
    a = client.post(
        "/login",
        json={"username": "nosuch", "password": "wrong-password"},
        headers={"X-CSRF-Token": token, "Accept": "application/json"},
    )
    b = client.post(
        "/login",
        json={"username": "admin", "password": "wrong-password"},
        headers={"X-CSRF-Token": token, "Accept": "application/json"},
    )
    assert a.status_code == b.status_code == 401
    assert a.json()["detail"] == b.json()["detail"] == LOGIN_MESSAGE


# ── HTTPS / cookies ─────────────────────────────────────────────────────────

def _set_cookie_header(response) -> str:
    headers = response.headers
    if hasattr(headers, "get_list"):
        parts = headers.get_list("set-cookie")
    elif hasattr(headers, "getlist"):
        parts = headers.getlist("set-cookie")
    else:
        raw = headers.get("set-cookie")
        parts = [raw] if raw else []
    return ",".join(parts).lower()


def test_forwarded_proto_https_sets_secure_cookie(client):
    token = _csrf(client)
    r = client.post(
        "/login",
        json={"username": "admin", "password": ADMIN_PW},
        headers={
            "X-CSRF-Token": token,
            "Accept": "application/json",
            "X-Forwarded-Proto": "https",
        },
    )
    assert r.status_code == 200
    assert "secure" in _set_cookie_header(r)


def test_plain_http_cookie_is_not_secure(client):
    r = _login(client, "admin", ADMIN_PW)
    assert r.status_code == 200
    assert "secure" not in _set_cookie_header(r)


def test_force_secure_cookies(client, monkeypatch):
    # Obtain CSRF over plain HTTP first; a Secure cookie would be discarded
    # by the HTTP test client (the same silent-login-loop as §6.3).
    token = _csrf(client)
    monkeypatch.setenv("REDIBIS_FORCE_SECURE_COOKIES", "1")
    r = client.post(
        "/login",
        json={"username": "admin", "password": ADMIN_PW},
        headers={"X-CSRF-Token": token, "Accept": "application/json"},
    )
    assert r.status_code == 200
    assert "secure" in _set_cookie_header(r)


def test_hsts_only_on_https(client):
    https = client.get("/health", headers={"X-Forwarded-Proto": "https"})
    assert "strict-transport-security" in https.headers
    http = client.get("/health")
    assert "strict-transport-security" not in http.headers


# ── Bootstrap ───────────────────────────────────────────────────────────────

def test_bootstrap_uses_admin_admin_when_no_users_json(tmp_path, capsys, caplog):
    store = AuthStore(JsonAuthBackend(tmp_path / "u.json"))
    caplog.set_level(logging.WARNING)
    result = store.bootstrap_admin()
    assert result["username"] == "admin"
    assert result["generated"] is False
    assert result["default_local"] is True
    out = capsys.readouterr().out
    assert "DEFAULT LOGIN" in out
    assert "admin" in out
    assert store.authenticate("admin", "admin") is not None
    assert store.has_legacy_admin_password() is True
    assert "empty users store" in caplog.text.lower() or "DEFAULT LOGIN" in caplog.text


def test_bootstrap_explicit_password_still_requires_length(tmp_path):
    store = AuthStore(JsonAuthBackend(tmp_path / "u.json"))
    with pytest.raises(ValueError, match="at least"):
        store.bootstrap_admin(username="admin", password="short")
    assert store.get_user("admin") is None


def test_bootstrap_explicit_env_password_wins(tmp_path):
    store = AuthStore(JsonAuthBackend(tmp_path / "u.json"))
    result = store.bootstrap_admin(username="admin", password="admin-password")
    assert result["default_local"] is False
    assert store.authenticate("admin", "admin-password") is not None
    assert store.authenticate("admin", "admin") is None


def test_bootstrap_inert_when_admin_exists(auth_store, caplog):
    auth_store.create_user("admin", "already-there1", role="admin")
    caplog.set_level(logging.WARNING)
    assert auth_store.bootstrap_admin(username="admin", password="new-password12") is None
    assert auth_store.authenticate("admin", "already-there1") is not None
    assert auth_store.authenticate("admin", "new-password12") is None
    assert "already exists" in caplog.text


def test_missing_canonical_json_ignores_legacy_meta_and_uses_admin_admin(
    tmp_path, caplog, monkeypatch,
):
    from redibis.webapp import store_accessors as sa

    monkeypatch.delenv("REDIBIS_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("REDIBIS_ADMIN_USER", raising=False)
    leftover = tmp_path / "storage" / "active-contracts" / "_meta" / "users.json"
    leftover.parent.mkdir(parents=True, exist_ok=True)
    leftover.write_text(
        '{"users":[{"username":"admin","password_hash":"stale","role":"admin",'
        '"default_scopes":["all"]}]}',
        encoding="utf-8",
    )
    caplog.set_level(logging.WARNING)
    sa.clear_stores()
    store = sa.get_auth_store()
    assert isinstance(store._backend, JsonAuthBackend)
    assert store.authenticate("admin", "admin") is not None
    assert (tmp_path / "auth" / "users.json").exists()
    assert "ignoring leftover" in caplog.text


def test_legacy_storage_bootstrap_names_contracts_bucket(tmp_path, caplog):
    store = AuthStore(StorageAuthBackend(LocalBackend(tmp_path), "active-contracts"))
    store.create_user("admin", "already-there1", role="admin")
    caplog.set_level(logging.WARNING)
    assert store.bootstrap_admin() is None
    assert "already exists" in caplog.text
    assert "active-contracts/_meta/users.json" in caplog.text


def test_bootstrap_never_resets_existing_password(auth_store):
    auth_store.create_user("keep", "original-pass1", role="admin")
    assert auth_store.bootstrap_admin(username="keep", password="reset-attempt1") is None
    assert auth_store.authenticate("keep", "original-pass1") is not None


def test_concurrent_bootstrap_keeps_one_password(tmp_path):
    """Two workers must not each print a password and clobber the hash."""
    path = tmp_path / "users.json"
    results: list[dict | None] = []
    errors: list[BaseException] = []

    def _boot(password: str) -> None:
        try:
            store = AuthStore(JsonAuthBackend(path))
            results.append(store.bootstrap_admin(username="admin", password=password))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=_boot, args=("first-password1",)),
        threading.Thread(target=_boot, args=("second-password2",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    created = [r for r in results if r is not None]
    skipped = [r for r in results if r is None]
    assert len(created) == 1
    assert len(skipped) == 1
    store = AuthStore(JsonAuthBackend(path))
    ok_first = store.authenticate("admin", "first-password1") is not None
    ok_second = store.authenticate("admin", "second-password2") is not None
    assert ok_first ^ ok_second
    assert len(store.list_users()) == 1


# ── Backends ────────────────────────────────────────────────────────────────

def test_same_suite_create_list_delete(auth_store):
    auth_store.create_user("a", "password-aaaa", role="explorer")
    assert auth_store.authenticate("a", "password-aaaa") is not None
    assert any(u["username"] == "a" for u in auth_store.list_users())
    assert auth_store.delete_user("a") is True
    assert auth_store.get_user("a") is None


def test_db_backend_empty_url_fails_loudly(monkeypatch):
    monkeypatch.setenv("REDIBIS_AUTH_ENABLED", "1")
    monkeypatch.setenv("REDIBIS_AUTH_BACKEND", "db")
    monkeypatch.setenv("REDIBIS_AUTH_DB", "")
    monkeypatch.delenv("REDIBIS_CONFIG", raising=False)
    with pytest.raises(ConfigError, match="refusing to fall back"):
        resolve_auth_config()
    with pytest.raises(ConfigError, match="refusing to fall back"):
        RedibisConfig.from_dict({"auth": {"backend": "db", "db_url": ""}})


def test_concurrent_save_user_does_not_lose_edits(tmp_path):
    backend = JsonAuthBackend(tmp_path / "users.json")
    errors: list[BaseException] = []

    def _save(name: str) -> None:
        try:
            backend.save_user(User(username=name, password_hash="h", role="explorer"))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_save, args=(f"u{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    users = backend.load_users()
    assert {f"u{i}" for i in range(8)} <= set(users)


def test_interrupted_write_leaves_previous_file(tmp_path, monkeypatch):
    path = tmp_path / "users.json"
    backend = JsonAuthBackend(path)
    backend.save_user(User(username="alice", password_hash="h", role="explorer"))
    original = path.read_text(encoding="utf-8")

    def _boom(src, dst):
        raise OSError("simulated crash")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError):
        backend.save_user(User(username="bob", password_hash="h", role="explorer"))
    assert path.read_text(encoding="utf-8") == original
    assert "alice" in original
    assert "bob" not in path.read_text(encoding="utf-8")


# ── Shares ──────────────────────────────────────────────────────────────────

def test_valid_share_reaches_route_without_session(client):
    from redibis.webapp.store_accessors import get_auth_store

    share = get_auth_store().create_share("demo.t", scope="pii", created_by="admin")
    r = client.get(f"/api/share/{share.share_token}")
    assert r.status_code != 401
    assert r.status_code != 303
    html = client.get(f"/share/{share.share_token}")
    assert html.status_code == 200


def test_expired_share_is_403(client):
    from redibis.webapp.store_accessors import get_auth_store

    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    share = get_auth_store().create_share(
        "demo.t", scope="all", created_by="admin", expires_at=past,
    )
    r = client.get(f"/api/share/{share.share_token}")
    assert r.status_code == 403


def test_share_scope_still_limits_fields():
    active = {"schema": [{"name": "t", "properties": [
        {"name": "email", "classification": "internal"},
    ]}]}
    edits = {"columns": {"email": {
        "classification": "pii_personal",
        "business": {"definition": "nope"},
    }}}
    modified, applied = apply_scoped_edits(active, edits, "pii")
    prop = modified["schema"][0]["properties"][0]
    assert prop["classification"] == "pii_personal"
    assert "business" not in prop
    assert applied == ["email.classification"]


def test_public_paths_constant():
    assert "/login" in PUBLIC_PATHS
    assert "/health" in PUBLIC_PATHS
    assert "/static/" in PUBLIC_PREFIXES


def test_expired_sessions_pruned_on_write(tmp_path):
    backend = JsonAuthBackend(tmp_path / "users.json")
    now = datetime.now(timezone.utc)
    past = (now - timedelta(hours=1)).isoformat()
    future = (now + timedelta(hours=12)).isoformat()
    payload = {
        "sessions": [
            {"token_hash": "dead1", "username": "a", "expires_at": past, "created_at": past},
            {"token_hash": "dead2", "username": "a", "expires_at": past, "created_at": past},
            {"token_hash": "alive", "username": "a", "expires_at": future, "created_at": past},
        ]
    }
    backend.sessions_path.write_text(json.dumps(payload), encoding="utf-8")
    backend.save_session(AuthSession(
        token_hash="new", username="a", expires_at=future,
    ))
    loaded = backend.load_sessions()
    assert set(loaded) == {"alive", "new"}


def test_session_file_bounded_after_1000_logins(tmp_path):
    backend = JsonAuthBackend(tmp_path / "users.json")
    now = datetime.now(timezone.utc)
    past = (now - timedelta(hours=1)).isoformat()
    future = (now + timedelta(hours=12)).isoformat()
    sessions = [
        {
            "token_hash": f"h{i}",
            "username": "a",
            "expires_at": past,
            "created_at": past,
        }
        for i in range(1000)
    ]
    backend.sessions_path.write_text(
        json.dumps({"sessions": sessions}), encoding="utf-8"
    )
    before = backend.sessions_path.stat().st_size
    backend.save_session(AuthSession(
        token_hash="live", username="a", expires_at=future,
    ))
    loaded = backend.load_sessions()
    assert list(loaded) == ["live"]
    after = backend.sessions_path.stat().st_size
    assert after < before
    assert after < 4096


def test_user_cache_invalidates_on_mtime_change(tmp_path):
    backend = JsonAuthBackend(tmp_path / "users.json")
    backend.save_user(User(username="alice", password_hash="h", role="explorer"))
    assert "alice" in backend.load_users()
    payload = {
        "users": [User(username="bob", password_hash="h", role="explorer").to_dict()]
    }
    backend.path.write_text(json.dumps(payload), encoding="utf-8")
    st = backend.path.stat()
    os.utime(backend.path, (st.st_mtime + 5, st.st_mtime + 5))
    users = backend.load_users()
    assert "bob" in users
    assert "alice" not in users


def test_safe_next_rejects_backslash_redirect():
    assert safe_next("/\\evil.com") == "/"
    assert safe_next("/\\/evil.com") == "/"
    assert safe_next("//evil.com") == "/"
    assert safe_next("https://evil.com") == "/"
    assert safe_next("/valid/path?x=1") == "/valid/path?x=1"


def test_csrf_header_required_outside_login_logout(client):
    assert _login(client, "admin", ADMIN_PW).status_code == 200
    r = client.post(
        "/admin/reset",
        headers={"X-CSRF-Token": "", "Accept": "application/json"},
    )
    assert r.status_code == 403
    assert r.json()["detail"] == CSRF_HEADER_REQUIRED


def test_no_plain_html_form_posts_outside_login_logout():
    root = Path(__file__).resolve().parents[1] / "redibis" / "webapp" / "templates"
    form_re = re.compile(r"<form\b[^>]*>", re.I)
    method_re = re.compile(r'\bmethod\s*=\s*["\']post["\']', re.I)
    action_re = re.compile(r'\baction\s*=\s*["\']([^"\']+)["\']', re.I)
    allowed = {"/login", "/logout"}
    offenders = []
    for path in root.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        for match in form_re.finditer(text):
            tag = match.group(0)
            if not method_re.search(tag):
                continue
            am = action_re.search(tag)
            action = am.group(1) if am else ""
            if action not in allowed:
                offenders.append(f"{path.name}: {tag}")
    assert offenders == []


def test_bootstrap_failure_logs_error_not_debug(tmp_path, caplog, monkeypatch):
    from redibis.webapp import store_accessors as sa

    class Boom:
        path = tmp_path / "u.json"

        def load_users(self):
            raise OSError("disk down")

    monkeypatch.setattr(sa, "build_auth_backend", lambda cfg=None: Boom())
    sa.clear_stores()
    caplog.set_level(logging.DEBUG)
    store = sa.get_auth_store()
    assert store is not None
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("FAILED" in (r.getMessage() or "") for r in errors)
    assert not any(
        r.levelno < logging.ERROR and "auth bootstrap skipped" in (r.getMessage() or "")
        for r in caplog.records
    )


def test_auth_warnings_report_default_admin_password(tmp_path, caplog, monkeypatch):
    from redibis.webapp.security import _emit_auth_warnings
    from redibis.webapp import store_accessors as sa

    store = AuthStore(JsonAuthBackend(tmp_path / "u.json"))
    store.bootstrap_admin()
    monkeypatch.setattr(sa, "get_auth_store", lambda: store)
    caplog.set_level(logging.WARNING)
    _emit_auth_warnings()
    assert "password 'admin'" in caplog.text


def test_auth_warnings_report_missing_admin(tmp_path, caplog, monkeypatch):
    from redibis.webapp.security import _emit_auth_warnings
    from redibis.webapp import store_accessors as sa

    empty = AuthStore(JsonAuthBackend(tmp_path / "u.json"))
    monkeypatch.setattr(sa, "get_auth_store", lambda: empty)
    caplog.set_level(logging.WARNING)
    _emit_auth_warnings()
    assert "no admin" in caplog.text.lower()


def test_cors_headers_on_auth_401(_auth_env):
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    from redibis.webapp.security import AuthMiddleware

    mini = FastAPI()
    mini.add_middleware(AuthMiddleware)
    mini.add_middleware(
        CORSMiddleware,
        allow_origins=["https://app.example.com"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @mini.get("/api/me")
    def _me():
        return {"ok": True}

    with TestClient(mini) as c:
        r = c.get(
            "/api/me",
            headers={
                "Origin": "https://app.example.com",
                "Accept": "application/json",
            },
        )
    assert r.status_code == 401
    assert r.headers.get("access-control-allow-origin") == "https://app.example.com"


def test_cors_middleware_wraps_auth_middleware():
    from starlette.middleware.cors import CORSMiddleware
    from redibis.webapp.backend import app
    from redibis.webapp.security import AuthMiddleware

    classes = [m.cls for m in app.user_middleware]
    assert classes.index(CORSMiddleware) < classes.index(AuthMiddleware)
