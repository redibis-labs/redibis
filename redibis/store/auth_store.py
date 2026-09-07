"""
redibis.store.auth_store — users, sessions, and scoped share links.

Storage is pluggable (``AuthBackend``): JSON file by default, SQLite/Postgres
when configured. Callers keep using ``get_auth_store().authenticate(...)``.

Passwords: PBKDF2-HMAC-SHA256 (stdlib). Legacy bare SHA-256 hashes still
verify and are upgraded on the next successful login.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger("redibis.store.auth")

ROLES = ("admin", "explorer")
SCOPES = ("business", "pii", "quality", "all")
MIN_PASSWORD_CHARS = 12
DEFAULT_LOCAL_ADMIN_USER = "admin"
DEFAULT_LOCAL_ADMIN_PASSWORD = "admin"
PBKDF2_ROUNDS = 200_000
PBKDF2_PREFIX = "pbkdf2_sha256$"

_logged_legacy_roles: set[str] = set()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().isoformat()


def map_stored_role(role: str, *, username: str = "") -> str:
    """Map on-disk roles to the two live roles. Unknown → explorer (fail closed)."""
    raw = (role or "").strip().lower()
    if raw == "admin":
        return "admin"
    if raw == "editor":
        if username and username not in _logged_legacy_roles:
            _logged_legacy_roles.add(username)
            logger.warning(
                "legacy role 'editor' for user %s mapped to 'admin' — review and demote deliberately",
                username,
            )
        return "admin"
    if raw in ("explorer", "viewer"):
        return "explorer"
    if raw:
        logger.warning(
            "unknown role %r for user %s mapped to 'explorer'", role, username or "?"
        )
    return "explorer"


def _pbkdf2_rounds() -> int:
    try:
        return int(os.environ.get("REDIBIS_PBKDF2_ROUNDS") or str(PBKDF2_ROUNDS))
    except ValueError:
        return PBKDF2_ROUNDS


def hash_password(password: str, *, salt_hex: str = "") -> str:
    salt = bytes.fromhex(salt_hex) if salt_hex else os.urandom(16)
    rounds = _pbkdf2_rounds()
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return f"{PBKDF2_PREFIX}{rounds}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> tuple[bool, bool]:
    """Returns ``(ok, needs_rehash)``."""
    stored = stored or ""
    if stored.startswith(PBKDF2_PREFIX):
        try:
            _algo, rounds_s, salt_hex, dk_hex = stored.split("$", 3)
            rounds = int(rounds_s)
        except ValueError:
            return False, False
        try:
            salt = bytes.fromhex(salt_hex)
            expected = bytes.fromhex(dk_hex)
        except ValueError:
            return False, False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
        ok = secrets.compare_digest(dk, expected)
        return ok, bool(ok and rounds != _pbkdf2_rounds())
    # Legacy unsalted SHA-256 hex digest.
    legacy = hashlib.sha256(password.encode("utf-8")).hexdigest()
    if len(stored) != len(legacy):
        return False, False
    if secrets.compare_digest(stored, legacy):
        return True, True
    return False, False


def session_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class User:
    username: str
    password_hash: str
    role: str = "explorer"  # admin | explorer
    default_scopes: list[str] = field(default_factory=lambda: ["all"])
    disabled: bool = False
    created_at: str = field(default_factory=_utc_now_iso)
    last_login_at: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    def public(self) -> dict:
        return {
            "username": self.username,
            "role": self.role,
            "default_scopes": self.default_scopes,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "User":
        username = d["username"]
        role = map_stored_role(d.get("role", "explorer"), username=username)
        return cls(
            username=username,
            password_hash=d["password_hash"],
            role=role,
            default_scopes=d.get("default_scopes", ["all"]),
            disabled=bool(d.get("disabled", False)),
            created_at=d.get("created_at") or _utc_now_iso(),
            last_login_at=d.get("last_login_at"),
        )


@dataclass
class Share:
    share_token: str
    table: str  # schema.table (human key)
    scope: str  # business | pii | quality | all
    created_by: str
    contract_uuid: Optional[str] = None
    granted_to: Optional[str] = None  # username, or None for link-only
    expires_at: Optional[str] = None  # ISO datetime, or None = no expiry
    created_at: str = field(default_factory=_utc_now_iso)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Share":
        return cls(
            share_token=d["share_token"],
            table=d["table"],
            scope=d.get("scope", "all"),
            created_by=d.get("created_by", ""),
            contract_uuid=d.get("contract_uuid"),
            granted_to=d.get("granted_to"),
            expires_at=d.get("expires_at"),
            created_at=d.get("created_at", _utc_now_iso()),
        )

    def is_expired(self) -> bool:
        if not self.expires_at:
            return False
        try:
            return datetime.fromisoformat(self.expires_at) < _utc_now()
        except Exception:
            return False


@dataclass
class AuthSession:
    token_hash: str
    username: str
    expires_at: str
    created_at: str = field(default_factory=_utc_now_iso)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "AuthSession":
        return cls(
            token_hash=d["token_hash"],
            username=d["username"],
            expires_at=d["expires_at"],
            created_at=d.get("created_at", _utc_now_iso()),
        )

    def is_expired(self) -> bool:
        try:
            return datetime.fromisoformat(self.expires_at) < _utc_now()
        except Exception:
            return True


# ── Store ───────────────────────────────────────────────────────────────────

class AuthStore:
    """Public auth API. Delegates persistence to an ``AuthBackend``."""

    def __init__(self, backend: Any, bucket: str = ""):
        # Backward compatible: AuthStore(StorageBackend, bucket=...) still works.
        if bucket and not hasattr(backend, "load_users"):
            from redibis.store.auth_backend import StorageAuthBackend

            self._backend = StorageAuthBackend(backend, bucket)
        else:
            self._backend = backend

    # ── Users ─────────────────────────────────────────────────────────────

    def _load_users(self) -> dict[str, User]:
        return self._backend.load_users()

    def create_user(
        self,
        username: str,
        password: str,
        role: str = "explorer",
        default_scopes: Optional[list[str]] = None,
    ) -> User:
        if role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}")
        if len(password or "") < MIN_PASSWORD_CHARS:
            raise ValueError(
                f"password must be at least {MIN_PASSWORD_CHARS} characters"
            )
        username = (username or "").strip()
        if not username:
            raise ValueError("username is required")
        user = User(
            username=username,
            password_hash=hash_password(password),
            role=role,
            default_scopes=default_scopes or ["all"],
        )
        self._backend.save_user(user)
        return user

    def get_user(self, username: str) -> Optional[User]:
        return self._load_users().get(username)

    def list_users(self) -> list[dict]:
        return [u.public() for u in self._load_users().values()]

    def delete_user(self, username: str) -> bool:
        target = self.get_user(username)
        if target is None:
            return False
        if target.role == "admin":
            admins = [u for u in self._load_users().values() if u.role == "admin"]
            if len(admins) <= 1:
                raise ValueError("cannot delete the last admin")
        return bool(self._backend.delete_user(username))

    def set_role(self, username: str, role: str) -> User:
        if role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}")
        user = self.get_user(username)
        if user is None:
            raise ValueError(f"user {username!r} not found")
        if user.role == "admin" and role != "admin":
            admins = [u for u in self._load_users().values() if u.role == "admin"]
            if len(admins) <= 1:
                raise ValueError("cannot demote the last admin")
        user.role = role
        self._backend.save_user(user)
        return user

    def set_password(self, username: str, password: str) -> User:
        if len(password or "") < MIN_PASSWORD_CHARS:
            raise ValueError(
                f"password must be at least {MIN_PASSWORD_CHARS} characters"
            )
        user = self.get_user(username)
        if user is None:
            raise ValueError(f"user {username!r} not found")
        user.password_hash = hash_password(password)
        self._backend.save_user(user)
        return user

    def authenticate(self, username: str, password: str) -> Optional[User]:
        user = self.get_user((username or "").strip())
        if user is None or user.disabled:
            return None
        ok, needs_rehash = verify_password(password, user.password_hash)
        if not ok:
            return None
        dirty = False
        if needs_rehash:
            user.password_hash = hash_password(password)
            dirty = True
        if user.role not in ROLES:
            user.role = map_stored_role(user.role, username=user.username)
            dirty = True
        user.last_login_at = _utc_now_iso()
        dirty = True
        if dirty:
            self._backend.save_user(user)
        return user

    def _warn_admin_exists(self) -> None:
        users = self._load_users()
        names = ", ".join(
            sorted(u.username for u in users.values() if u.role == "admin")
        )
        loc = getattr(self._backend, "path", None)
        loc_s = f" in {loc}" if loc else ""
        logger.warning(
            "auth: admin already exists (%s)%s; password is not printed again. "
            "Delete that file and restart to generate a new one, or sign in "
            "and change it at /users.",
            names or "admin",
            loc_s,
        )

    def bootstrap_admin(
        self,
        username: str = "",
        password: str = "",
    ) -> Optional[dict]:
        """Create the first admin. Inert once any admin exists.

        Empty store + no password (no ``users.json`` / no env) uses the local
        default ``admin`` / ``admin``. Explicit ``REDIBIS_ADMIN_PASSWORD`` still
        wins and must be at least ``MIN_PASSWORD_CHARS`` unless it is exactly
        that well-known default.
        """
        users = self._load_users()
        if any(u.role == "admin" for u in users.values()):
            self._warn_admin_exists()
            return None
        user_name = (username or "").strip() or DEFAULT_LOCAL_ADMIN_USER
        default_local = False
        if not (password or "").strip():
            password = DEFAULT_LOCAL_ADMIN_PASSWORD
            default_local = True
        elif password == DEFAULT_LOCAL_ADMIN_PASSWORD:
            default_local = True
        elif len(password) < MIN_PASSWORD_CHARS:
            raise ValueError(
                f"bootstrap admin password must be at least {MIN_PASSWORD_CHARS} characters"
            )
        candidate = User(
            username=user_name,
            password_hash=hash_password(password),
            role="admin",
            default_scopes=["all"],
        )
        ensure = getattr(self._backend, "create_first_admin", None)
        created = ensure(candidate) if ensure is not None else None
        if created is False:
            self._warn_admin_exists()
            return None
        if created is None:
            if default_local:
                self._backend.save_user(candidate)
                user = candidate
            else:
                user = self.create_user(
                    user_name, password, role="admin", default_scopes=["all"]
                )
        else:
            user = candidate
        if default_local:
            banner = (
                "\n"
                "**********************************************************************\n"
                "REDIBIS DEFAULT LOGIN (empty users store — change this now)\n"
                f"  username: {user.username}\n"
                f"  password: {DEFAULT_LOCAL_ADMIN_PASSWORD}\n"
                "This warning repeats until you change the password at /users.\n"
                "**********************************************************************\n"
            )
            print(banner, flush=True)
            logger.warning(
                "REDIBIS DEFAULT LOGIN (empty users store — change this now) "
                "username=%s password=%s",
                user.username,
                DEFAULT_LOCAL_ADMIN_PASSWORD,
            )
        return {
            "username": user.username,
            "role": user.role,
            "generated": False,
            "default_local": default_local,
        }

    def ensure_default_admin(
        self, username: str = "", password: str = ""
    ) -> Optional[User]:
        """Deprecated alias for ``bootstrap_admin``."""
        result = self.bootstrap_admin(username=username, password=password)
        if result is None:
            return None
        return self.get_user(result["username"])

    def has_admin(self) -> bool:
        return any(u.role == "admin" for u in self._load_users().values())

    def has_legacy_admin_password(self) -> bool:
        """True when a user named ``admin`` still verifies against ``admin``."""
        user = self.get_user("admin")
        if user is None:
            return False
        ok, _ = verify_password("admin", user.password_hash)
        return ok

    def count_legacy_hashes(self) -> int:
        return sum(
            1
            for u in self._load_users().values()
            if not (u.password_hash or "").startswith(PBKDF2_PREFIX)
        )

    def startup_audit(self) -> None:
        users = self._load_users()
        for u in users.values():
            # Re-run mapping so editor warnings fire at startup even if from_dict
            # already canonicalised — from_dict logs editor on first map.
            map_stored_role(u.role, username=u.username)
        n_legacy = self.count_legacy_hashes()
        if n_legacy:
            logger.warning(
                "%s user password hash(es) still use legacy SHA-256; they will "
                "upgrade on next login",
                n_legacy,
            )

    # ── Sessions ──────────────────────────────────────────────────────────

    def create_session(self, username: str, hours: float = 12) -> str:
        token = secrets.token_urlsafe(32)
        expires = _utc_now() + timedelta(hours=float(hours))
        session = AuthSession(
            token_hash=session_token_hash(token),
            username=username,
            expires_at=expires.isoformat(),
        )
        self._backend.save_session(session)
        return token

    def get_session_user(self, token: str) -> Optional[User]:
        if not token:
            return None
        digest = session_token_hash(token)
        session = self._backend.load_sessions().get(digest)
        if session is None or session.is_expired():
            if session is not None:
                self._backend.delete_session(digest)
            return None
        user = self.get_user(session.username)
        if user is None or user.disabled:
            return None
        user.role = map_stored_role(user.role, username=user.username)
        return user

    def delete_session(self, token: str) -> bool:
        if not token:
            return False
        return bool(self._backend.delete_session(session_token_hash(token)))

    # ── Shares ────────────────────────────────────────────────────────────

    def create_share(
        self,
        table: str,
        scope: str,
        created_by: str,
        contract_uuid: Optional[str] = None,
        granted_to: Optional[str] = None,
        expires_at: Optional[str] = None,
    ) -> Share:
        if scope not in SCOPES:
            raise ValueError(f"scope must be one of {SCOPES}")
        share = Share(
            share_token=secrets.token_urlsafe(24),
            table=table,
            scope=scope,
            created_by=created_by,
            contract_uuid=contract_uuid,
            granted_to=granted_to,
            expires_at=expires_at,
        )
        self._backend.save_share(share)
        return share

    def get_share(self, token: str) -> Optional[Share]:
        return self._backend.load_shares().get(token)

    def list_shares(self, table: Optional[str] = None) -> list[dict]:
        return [
            s.to_dict()
            for s in self._backend.load_shares().values()
            if table is None or s.table == table
        ]

    def revoke_share(self, token: str) -> bool:
        return bool(self._backend.delete_share(token))


# ── Scoped editing (share edits apply directly to the active contract) ─────────

# Which property-level fields each scope may write.
_SCOPE_PROP_FIELDS = {
    "business": {"business"},
    "pii": {"classification", "pii", "privacy", "maskingPolicy"},
    "quality": {"quality"},
    "all": {
        "business",
        "classification",
        "pii",
        "privacy",
        "maskingPolicy",
        "quality",
        "tags",
        "description",
        "businessName",
        "logicalType",
        "physicalType",
        "required",
        "unique",
    },
}


def scope_allows(scope: str, field: str) -> bool:
    return field in _SCOPE_PROP_FIELDS.get(scope, set())


def apply_scoped_edits(active: dict, edits: dict, scope: str) -> tuple[dict, list[str]]:
    """
    Apply scope-limited edits to a copy of the active contract.

    ``edits`` shape:
        {
          "table_tags": [...],                      # quality/all not required; tags = all/business
          "columns": {"<col>": {"<field>": <value>, ...}},
          "table_quality": [...]                    # quality | all only
        }

    Returns (modified_contract, applied_field_labels). Fields outside the
    scope are silently skipped (caller may inspect the applied list).
    """
    import copy

    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {SCOPES}")
    contract = copy.deepcopy(active)
    applied: list[str] = []
    allowed = _SCOPE_PROP_FIELDS[scope]

    columns_edit = edits.get("columns", {}) or {}
    table_quality = edits.get("table_quality")
    table_tags = edits.get("table_tags")

    for schema_obj in contract.get("schema", []) or []:
        if table_quality is not None and ("quality" in allowed):
            schema_obj["quality"] = table_quality
            applied.append("table.quality")
        if table_tags is not None and ("tags" in allowed or scope in ("business", "all")):
            existing = set(schema_obj.get("tags", []) or [])
            schema_obj["tags"] = sorted(existing | set(table_tags))
            applied.append("table.tags")
        for prop in schema_obj.get("properties", []) or []:
            col = prop.get("name")
            ce = columns_edit.get(col)
            if not ce:
                continue
            for fld, val in ce.items():
                if fld == "tags" and (scope in ("business", "all")):
                    existing = set(prop.get("tags", []) or [])
                    prop["tags"] = sorted(existing | set(val or []))
                    applied.append(f"{col}.tags")
                elif fld in allowed:
                    prop[fld] = val
                    applied.append(f"{col}.{fld}")
                # else: outside scope → skip
    return contract, applied
