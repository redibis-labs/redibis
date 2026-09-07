"""Auth storage backends: JSON (default) and SQLite/Postgres.

``AuthStore`` delegates persistence here. An ``OidcAuthBackend`` can be added
later without touching webapp routes.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional, Protocol, Sequence

from redibis.store.auth_store import AuthSession, Share, User, _utc_now_iso

logger = logging.getLogger("redibis.store.auth")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS auth_users (
  username       TEXT PRIMARY KEY,
  password_hash  TEXT NOT NULL,
  role           TEXT NOT NULL DEFAULT 'explorer',
  default_scopes TEXT NOT NULL DEFAULT 'all',
  disabled       INTEGER NOT NULL DEFAULT 0,
  created_at     TEXT NOT NULL,
  last_login_at  TEXT
);
CREATE TABLE IF NOT EXISTS auth_shares (
  share_token   TEXT PRIMARY KEY,
  table_name    TEXT NOT NULL,
  scope         TEXT NOT NULL,
  created_by    TEXT NOT NULL,
  contract_uuid TEXT,
  granted_to    TEXT,
  expires_at    TEXT,
  created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_sessions (
  token_hash  TEXT PRIMARY KEY,
  username    TEXT NOT NULL,
  expires_at  TEXT NOT NULL,
  created_at  TEXT NOT NULL
);
"""


class AuthBackend(Protocol):
    def load_users(self) -> dict[str, User]: ...
    def save_user(self, user: User) -> None: ...
    def delete_user(self, username: str) -> bool: ...
    def load_shares(self) -> dict[str, Share]: ...
    def save_share(self, share: Share) -> None: ...
    def delete_share(self, token: str) -> bool: ...
    def load_sessions(self) -> dict[str, AuthSession]: ...
    def save_session(self, session: AuthSession) -> None: ...
    def delete_session(self, token_hash: str) -> bool: ...


# ── Advisory lock + atomic JSON replace ─────────────────────────────────────

@contextmanager
def _advisory_lock(lock_path: Path) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+")
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        fh.close()


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    data = json.dumps(payload, indent=2).encode("utf-8")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(str(tmp), str(path))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.exception("failed to read auth file %s", path)
        return {}
    return raw if isinstance(raw, dict) else {}


def _live_sessions(sessions: dict[str, AuthSession]) -> dict[str, AuthSession]:
    return {k: s for k, s in sessions.items() if not s.is_expired()}


def _path_mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime if path.exists() else None
    except OSError:
        return None


class _MtimeCache:
    """Re-read only when the file actually changed.

    Correct on a single node. Two dashboard replicas sharing a JSON file
    would each cache independently and could miss a peer's logout.
    Multi-replica deployments must use ``auth.backend: db``.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._mtime: float | None = None
        self._data: Any = None

    def get(self, path: Path, loader):
        mtime = _path_mtime(path)
        with self._lock:
            if self._data is not None and mtime == self._mtime:
                return self._data
        data = loader()
        with self._lock:
            self._mtime = mtime
            self._data = data
        return data

    def store(self, path: Path, data) -> None:
        with self._lock:
            self._mtime = _path_mtime(path)
            self._data = data

    def invalidate(self) -> None:
        with self._lock:
            self._mtime = None
            self._data = None


# ── JSON backend (default) ──────────────────────────────────────────────────

class JsonAuthBackend:
    """Filesystem users/shares/sessions with flock + atomic replace.

    Assumes a single dashboard process. Replicas sharing this file each keep
    an mtime cache and can miss a peer's logout; use ``auth.backend: db``
    when more than one process must coordinate.
    """

    def __init__(self, store_path: str | Path):
        self.path = Path(store_path)
        self.shares_path = self.path.with_name("shares.json")
        self.sessions_path = self.path.with_name("sessions.json")
        self._lock_path = self.path.with_name(self.path.name + ".lock")
        self._users_cache = _MtimeCache()
        self._sessions_cache = _MtimeCache()

    def _read_users(self) -> dict[str, User]:
        raw = _read_json(self.path)
        return {u["username"]: User.from_dict(u) for u in raw.get("users", []) if "username" in u}

    def load_users(self) -> dict[str, User]:
        return dict(self._users_cache.get(self.path, self._read_users))

    def save_user(self, user: User) -> None:
        with _advisory_lock(self._lock_path):
            users = self._read_users()
            users[user.username] = user
            _atomic_write_json(
                self.path, {"users": [u.to_dict() for u in users.values()]}
            )
            self._users_cache.store(self.path, dict(users))

    def create_first_admin(self, user: User) -> bool:
        """Atomically create the first admin. False if one already exists.

        Holds the same flock as ``save_user`` so two uvicorn workers cannot
        each print a password and overwrite the other's hash.
        """
        with _advisory_lock(self._lock_path):
            users = self._read_users()
            if any(u.role == "admin" for u in users.values()):
                return False
            users[user.username] = user
            _atomic_write_json(
                self.path, {"users": [u.to_dict() for u in users.values()]}
            )
            self._users_cache.store(self.path, dict(users))
            return True

    def delete_user(self, username: str) -> bool:
        with _advisory_lock(self._lock_path):
            users = self._read_users()
            if username not in users:
                return False
            del users[username]
            _atomic_write_json(
                self.path, {"users": [u.to_dict() for u in users.values()]}
            )
            self._users_cache.store(self.path, dict(users))
            return True

    def load_shares(self) -> dict[str, Share]:
        raw = _read_json(self.shares_path)
        return {
            s["share_token"]: Share.from_dict(s)
            for s in raw.get("shares", [])
            if "share_token" in s
        }

    def save_share(self, share: Share) -> None:
        with _advisory_lock(self._lock_path):
            shares = self.load_shares()
            shares[share.share_token] = share
            _atomic_write_json(
                self.shares_path, {"shares": [s.to_dict() for s in shares.values()]}
            )

    def delete_share(self, token: str) -> bool:
        with _advisory_lock(self._lock_path):
            shares = self.load_shares()
            if token not in shares:
                return False
            del shares[token]
            _atomic_write_json(
                self.shares_path, {"shares": [s.to_dict() for s in shares.values()]}
            )
            return True

    def _read_sessions(self) -> dict[str, AuthSession]:
        raw = _read_json(self.sessions_path)
        return {
            s["token_hash"]: AuthSession.from_dict(s)
            for s in raw.get("sessions", [])
            if "token_hash" in s
        }

    def load_sessions(self) -> dict[str, AuthSession]:
        return dict(self._sessions_cache.get(self.sessions_path, self._read_sessions))

    def save_session(self, session: AuthSession) -> None:
        with _advisory_lock(self._lock_path):
            sessions = _live_sessions(self._read_sessions())
            sessions[session.token_hash] = session
            _atomic_write_json(
                self.sessions_path,
                {"sessions": [s.to_dict() for s in sessions.values()]},
            )
            self._sessions_cache.store(self.sessions_path, dict(sessions))

    def delete_session(self, token_hash: str) -> bool:
        with _advisory_lock(self._lock_path):
            sessions = _live_sessions(self._read_sessions())
            existed = token_hash in sessions
            sessions.pop(token_hash, None)
            _atomic_write_json(
                self.sessions_path,
                {"sessions": [s.to_dict() for s in sessions.values()]},
            )
            self._sessions_cache.store(self.sessions_path, dict(sessions))
            return existed


# ── Legacy contracts-bucket backend ─────────────────────────────────────────

class StorageAuthBackend:
    """Read/write ``_meta/users.json`` (and siblings) in the contracts bucket.

    Used only when a legacy file already exists and ``auth.store_path`` has not
    been created. Never relocates the file.
    """

    USERS_KEY = "_meta/users.json"
    SHARES_KEY = "_meta/shares.json"
    SESSIONS_KEY = "_meta/sessions.json"

    def __init__(self, storage: Any, bucket: str):
        self.storage = storage
        self.bucket = bucket
        self.path = f"{bucket}/{self.USERS_KEY}"
        self._lock = threading.RLock()
        self._users_cache: dict[str, User] | None = None
        self._sessions_cache: dict[str, AuthSession] | None = None

    def _get(self, key: str) -> dict:
        if not self.storage.exists(self.bucket, key):
            return {}
        raw = self.storage.get_json(self.bucket, key)
        return raw if isinstance(raw, dict) else {}

    def _read_users(self) -> dict[str, User]:
        raw = self._get(self.USERS_KEY)
        return {u["username"]: User.from_dict(u) for u in raw.get("users", []) if "username" in u}

    def load_users(self) -> dict[str, User]:
        with self._lock:
            if self._users_cache is None:
                self._users_cache = self._read_users()
            return dict(self._users_cache)

    def save_user(self, user: User) -> None:
        with self._lock:
            users = self._read_users()
            users[user.username] = user
            self.storage.put_json(
                self.bucket, self.USERS_KEY,
                {"users": [u.to_dict() for u in users.values()]},
            )
            self._users_cache = dict(users)

    def create_first_admin(self, user: User) -> bool:
        with self._lock:
            users = self._read_users()
            if any(u.role == "admin" for u in users.values()):
                return False
            users[user.username] = user
            self.storage.put_json(
                self.bucket, self.USERS_KEY,
                {"users": [u.to_dict() for u in users.values()]},
            )
            self._users_cache = dict(users)
            return True

    def delete_user(self, username: str) -> bool:
        with self._lock:
            users = self._read_users()
            if username not in users:
                return False
            del users[username]
            self.storage.put_json(
                self.bucket, self.USERS_KEY,
                {"users": [u.to_dict() for u in users.values()]},
            )
            self._users_cache = dict(users)
            return True

    def load_shares(self) -> dict[str, Share]:
        raw = self._get(self.SHARES_KEY)
        return {
            s["share_token"]: Share.from_dict(s)
            for s in raw.get("shares", [])
            if "share_token" in s
        }

    def save_share(self, share: Share) -> None:
        with self._lock:
            shares = self.load_shares()
            shares[share.share_token] = share
            self.storage.put_json(
                self.bucket, self.SHARES_KEY,
                {"shares": [s.to_dict() for s in shares.values()]},
            )

    def delete_share(self, token: str) -> bool:
        with self._lock:
            shares = self.load_shares()
            if token not in shares:
                return False
            del shares[token]
            self.storage.put_json(
                self.bucket, self.SHARES_KEY,
                {"shares": [s.to_dict() for s in shares.values()]},
            )
            return True

    def _read_sessions(self) -> dict[str, AuthSession]:
        raw = self._get(self.SESSIONS_KEY)
        return {
            s["token_hash"]: AuthSession.from_dict(s)
            for s in raw.get("sessions", [])
            if "token_hash" in s
        }

    def load_sessions(self) -> dict[str, AuthSession]:
        with self._lock:
            if self._sessions_cache is None:
                self._sessions_cache = self._read_sessions()
            return dict(self._sessions_cache)

    def save_session(self, session: AuthSession) -> None:
        with self._lock:
            sessions = _live_sessions(self._read_sessions())
            sessions[session.token_hash] = session
            self.storage.put_json(
                self.bucket, self.SESSIONS_KEY,
                {"sessions": [s.to_dict() for s in sessions.values()]},
            )
            self._sessions_cache = dict(sessions)

    def delete_session(self, token_hash: str) -> bool:
        with self._lock:
            sessions = _live_sessions(self._read_sessions())
            existed = token_hash in sessions
            sessions.pop(token_hash, None)
            self.storage.put_json(
                self.bucket, self.SESSIONS_KEY,
                {"sessions": [s.to_dict() for s in sessions.values()]},
            )
            self._sessions_cache = dict(sessions)
            return existed


# ── DB backend (stdlib sqlite3; psycopg lazy for postgres://) ───────────────

class _AuthDb:
    def __init__(self, url: str):
        self.url = url or "sqlite:///:memory:"
        self.kind = "postgres" if self.url.startswith("postgres") else "sqlite"
        self._lock = threading.RLock()
        self._mem: Optional[sqlite3.Connection] = None
        if self.kind == "sqlite" and (
            ":memory:" in self.url or self.url in ("sqlite://", "sqlite:///")
        ):
            conn = sqlite3.connect(":memory:", check_same_thread=False)
            conn.row_factory = sqlite3.Row
            self._mem = conn

    def _sqlite_path(self) -> str:
        raw = self.url
        for prefix in ("sqlite:///", "sqlite://", "sqlite:"):
            if raw.startswith(prefix):
                path = raw[len(prefix):]
                return path or ":memory:"
        return raw

    def connect(self) -> Any:
        if self.kind == "postgres":
            try:
                import psycopg
                from psycopg.rows import dict_row
            except ImportError as exc:
                raise RuntimeError("psycopg is required for postgres:// auth URLs") from exc
            return psycopg.connect(self.url, row_factory=dict_row)
        if self._mem is not None:
            return self._mem
        path = self._sqlite_path()
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def q(self, sql: str) -> str:
        if self.kind == "postgres":
            return sql.replace("?", "%s")
        return sql

    def init_schema(self) -> None:
        conn = self.connect()
        own = self.kind == "postgres" or self._mem is None
        try:
            if self.kind == "sqlite":
                conn.executescript(SCHEMA_SQL)
            else:
                conn.execute(SCHEMA_SQL)
            if own or self.kind == "sqlite":
                conn.commit()
        finally:
            if own:
                conn.close()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        def _run() -> list[dict[str, Any]]:
            conn = self.connect()
            own = self.kind == "postgres" or self._mem is None
            try:
                cur = conn.execute(self.q(sql), tuple(params))
                rows = [] if cur.description is None else cur.fetchall()
                if own or self.kind == "sqlite":
                    conn.commit()
                return [_row_dict(r) for r in rows]
            finally:
                if own:
                    conn.close()

        if self.kind == "sqlite":
            with self._lock:
                return _run()
        return _run()


def _row_dict(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    return {k: row[k] for k in row.keys()}


def _scopes_to_db(scopes: list[str] | None) -> str:
    return ",".join(scopes or ["all"])


def _scopes_from_db(raw: str | None) -> list[str]:
    if not raw:
        return ["all"]
    return [p.strip() for p in str(raw).split(",") if p.strip()] or ["all"]


class DbAuthBackend:
    """SQLite or Postgres. Selected by config; never as a silent fallback."""

    def __init__(self, db_url: str):
        if not (db_url or "").strip():
            from redibis.config import ConfigError

            raise ConfigError(
                "auth.backend is 'db' but auth.db_url / REDIBIS_AUTH_DB is empty; "
                "refusing to fall back to JSON"
            )
        self.db = _AuthDb(db_url.strip())
        self.db.init_schema()

    def load_users(self) -> dict[str, User]:
        rows = self.db.execute("SELECT * FROM auth_users")
        out: dict[str, User] = {}
        for r in rows:
            out[r["username"]] = User.from_dict(
                {
                    "username": r["username"],
                    "password_hash": r["password_hash"],
                    "role": r.get("role") or "explorer",
                    "default_scopes": _scopes_from_db(r.get("default_scopes")),
                    "disabled": bool(r.get("disabled")),
                    "created_at": r.get("created_at") or "",
                    "last_login_at": r.get("last_login_at"),
                }
            )
        return out

    def save_user(self, user: User) -> None:
        self.db.execute(
            "INSERT INTO auth_users (username, password_hash, role, default_scopes, "
            "disabled, created_at, last_login_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (username) DO UPDATE SET password_hash = excluded.password_hash, "
            "role = excluded.role, default_scopes = excluded.default_scopes, "
            "disabled = excluded.disabled, created_at = excluded.created_at, "
            "last_login_at = excluded.last_login_at",
            (
                user.username,
                user.password_hash,
                user.role,
                _scopes_to_db(user.default_scopes),
                1 if user.disabled else 0,
                user.created_at,
                user.last_login_at,
            ),
        )

    def create_first_admin(self, user: User) -> bool:
        existing = self.db.execute(
            "SELECT username FROM auth_users WHERE role = ?", ("admin",)
        )
        if existing:
            return False
        self.save_user(user)
        return True

    def delete_user(self, username: str) -> bool:
        before = self.db.execute(
            "SELECT username FROM auth_users WHERE username = ?", (username,)
        )
        if not before:
            return False
        self.db.execute("DELETE FROM auth_users WHERE username = ?", (username,))
        return True

    def load_shares(self) -> dict[str, Share]:
        rows = self.db.execute("SELECT * FROM auth_shares")
        out: dict[str, Share] = {}
        for r in rows:
            share = Share(
                share_token=r["share_token"],
                table=r["table_name"],
                scope=r["scope"],
                created_by=r["created_by"],
                contract_uuid=r.get("contract_uuid"),
                granted_to=r.get("granted_to"),
                expires_at=r.get("expires_at"),
                created_at=r.get("created_at") or "",
            )
            out[share.share_token] = share
        return out

    def save_share(self, share: Share) -> None:
        self.db.execute(
            "INSERT INTO auth_shares (share_token, table_name, scope, created_by, "
            "contract_uuid, granted_to, expires_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (share_token) DO UPDATE SET table_name = excluded.table_name, "
            "scope = excluded.scope, created_by = excluded.created_by, "
            "contract_uuid = excluded.contract_uuid, granted_to = excluded.granted_to, "
            "expires_at = excluded.expires_at, created_at = excluded.created_at",
            (
                share.share_token,
                share.table,
                share.scope,
                share.created_by,
                share.contract_uuid,
                share.granted_to,
                share.expires_at,
                share.created_at,
            ),
        )

    def delete_share(self, token: str) -> bool:
        before = self.db.execute(
            "SELECT share_token FROM auth_shares WHERE share_token = ?", (token,)
        )
        if not before:
            return False
        self.db.execute("DELETE FROM auth_shares WHERE share_token = ?", (token,))
        return True

    def load_sessions(self) -> dict[str, AuthSession]:
        rows = self.db.execute("SELECT * FROM auth_sessions")
        return {
            r["token_hash"]: AuthSession(
                token_hash=r["token_hash"],
                username=r["username"],
                expires_at=r["expires_at"],
                created_at=r.get("created_at") or "",
            )
            for r in rows
        }

    def save_session(self, session: AuthSession) -> None:
        self.db.execute(
            "DELETE FROM auth_sessions WHERE expires_at <= ?",
            (_utc_now_iso(),),
        )
        self.db.execute(
            "INSERT INTO auth_sessions (token_hash, username, expires_at, created_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT (token_hash) DO UPDATE SET username = excluded.username, "
            "expires_at = excluded.expires_at, created_at = excluded.created_at",
            (session.token_hash, session.username, session.expires_at, session.created_at),
        )

    def delete_session(self, token_hash: str) -> bool:
        before = self.db.execute(
            "SELECT token_hash FROM auth_sessions WHERE token_hash = ?", (token_hash,)
        )
        if not before:
            return False
        self.db.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (token_hash,))
        return True
