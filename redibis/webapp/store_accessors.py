"""Lazy, self-healing storage and service singletons for the webapp adapter."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, TypeVar

from redibis.config import AuthConfig, ConfigError, RedibisConfig
from redibis.store.auth_store import AuthStore
from redibis.store.config_store import LocalConfigStore, ObjectConfigStore
from redibis.store.contract_store import ContractStore
from redibis.store.run_merger import RunMerger
from redibis.store.storage_backend import LocalBackend, StorageBackend, get_backend
from redibis.store.subcontract_store import SubcontractStore

logger = logging.getLogger("redibis.webapp.stores")

_stores: dict[str, Any] = {}
_last_ping: float = 0.0
_PING_TTL = float(os.getenv("REDIBIS_BACKEND_PING_TTL", "10"))

T = TypeVar("T")


def _runs_bucket() -> str:
    return os.getenv("S3_RUNS_BUCKET", "pii-reports")


def _contracts_bucket() -> str:
    return os.getenv("S3_CONTRACTS_BUCKET", "active-contracts")


def get_runs_bucket() -> str:
    return _runs_bucket()


def get_contracts_bucket() -> str:
    return _contracts_bucket()


def _build_storage() -> StorageBackend:
    if os.getenv("USE_LOCAL_STORAGE", "false").lower() == "true":
        return LocalBackend(os.getenv("LOCAL_STORAGE_ROOT", "./_local_storage"))
    return get_backend(mode="auto")


def _contract_store_memory_config():
    cfg_path = os.environ.get("REDIBIS_CONFIG")
    if not cfg_path:
        return None
    from redibis.config import RedibisConfig

    return RedibisConfig.from_yaml(cfg_path).memory


def _invalidate_dependent_stores() -> None:
    for key in ("contract", "subcontract", "run_merger", "auth", "config"):
        _stores.pop(key, None)


def clear_stores() -> None:
    """Drop cached handles (operator reset / reconnect)."""
    global _last_ping
    _stores.clear()
    _last_ping = 0.0


def get_backend_store() -> StorageBackend:
    global _last_ping
    st = _stores.get("backend")
    now = time.monotonic()
    if st is not None and (now - _last_ping < _PING_TTL or st.ping()):
        _last_ping = now
        return st
    _invalidate_dependent_stores()
    st = _build_storage()
    _stores["backend"] = st
    _last_ping = now
    return st


def get_contract_store() -> ContractStore:
    st = _stores.get("contract")
    if st is not None:
        return st
    backend = get_backend_store()
    st = ContractStore(
        backend,
        bucket=_contracts_bucket(),
        memory_config=_contract_store_memory_config(),
    )
    _stores["contract"] = st
    return st


def get_subcontract_store() -> SubcontractStore:
    st = _stores.get("subcontract")
    if st is not None:
        return st
    st = SubcontractStore(
        get_backend_store(),
        pii_bucket=os.getenv("S3_PII_RUNS_BUCKET", "pii-contracts"),
        quality_bucket=os.getenv("S3_QUALITY_RUNS_BUCKET", "quality-contracts"),
    )
    _stores["subcontract"] = st
    return st


def get_run_merger() -> RunMerger:
    st = _stores.get("run_merger")
    if st is not None:
        return st
    st = RunMerger(get_contract_store(), get_subcontract_store())
    _stores["run_merger"] = st
    return st


def _env_override(name: str, current: str) -> str:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return current
    return str(raw).strip()


def resolve_auth_config() -> AuthConfig:
    """YAML ``auth:`` block overlaid with ``REDIBIS_*`` env vars."""
    try:
        cfg_path = os.environ.get("REDIBIS_CONFIG")
        auth = (
            RedibisConfig.from_yaml(cfg_path).auth
            if cfg_path
            else RedibisConfig.default().auth
        )
    except Exception:
        auth = AuthConfig()
    enabled = auth.enabled
    if os.environ.get("REDIBIS_AUTH_ENABLED") is not None:
        enabled = os.environ.get("REDIBIS_AUTH_ENABLED", "").strip().lower() in {
            "1", "true", "yes", "on",
        }
    backend = (_env_override("REDIBIS_AUTH_BACKEND", auth.backend) or "json").lower()
    store_path = _env_override("REDIBIS_AUTH_PATH", auth.store_path) or "./configs/auth/users.json"
    db_url = _env_override("REDIBIS_AUTH_DB", auth.db_url)
    hours_raw = os.environ.get("REDIBIS_SESSION_HOURS")
    try:
        session_hours = float(hours_raw) if hours_raw else float(auth.session_hours or 12)
    except ValueError:
        session_hours = 12.0
    user = _env_override("REDIBIS_ADMIN_USER", auth.bootstrap_admin_user)
    password = _env_override("REDIBIS_ADMIN_PASSWORD", auth.bootstrap_admin_password)
    if backend == "db" and not db_url:
        raise ConfigError(
            "auth.backend is 'db' but auth.db_url / REDIBIS_AUTH_DB is empty; "
            "refusing to fall back to JSON"
        )
    return AuthConfig(
        enabled=enabled,
        backend=backend,
        store_path=store_path,
        db_url=db_url,
        session_hours=session_hours,
        bootstrap_admin_user=user,
        bootstrap_admin_password=password,
    )


def build_auth_backend(cfg: AuthConfig | None = None):
    cfg = cfg or resolve_auth_config()
    if cfg.backend == "db":
        from redibis.store.auth_backend import DbAuthBackend

        return DbAuthBackend(cfg.db_url)
    from pathlib import Path

    from redibis.store.auth_backend import JsonAuthBackend

    path = Path(cfg.store_path)
    if path.exists():
        return JsonAuthBackend(path)
    # Leftover hashes in the contracts bucket must not block empty-store
    # admin/admin. Warn, then create configs/auth/users.json on bootstrap.
    try:
        storage = get_backend_store()
        bucket = _contracts_bucket()
        if storage.exists(bucket, "_meta/users.json"):
            logger.warning(
                "ignoring leftover %s/_meta/users.json because %s is missing; "
                "bootstrapping a new local admin. Copy that leftover file to %s "
                "only if you still need those accounts.",
                bucket,
                path,
                path,
            )
    except Exception:
        logger.debug("legacy auth probe skipped", exc_info=True)
    return JsonAuthBackend(path)


def get_auth_store() -> AuthStore:
    st = _stores.get("auth")
    if st is not None:
        return st
    cfg = resolve_auth_config()
    st = AuthStore(build_auth_backend(cfg))
    _stores["auth"] = st
    try:
        result = st.bootstrap_admin(
            username=cfg.bootstrap_admin_user,
            password=cfg.bootstrap_admin_password,
        )
        st.startup_audit()
        loc = getattr(st._backend, "path", None) or cfg.db_url or cfg.backend
        logger.info(
            "auth backend=%s store=%s",
            type(st._backend).__name__,
            loc,
        )
        if result and result.get("default_local"):
            logger.warning(
                "auth: empty users store — default login is admin/admin; "
                "change it at /users"
            )
        elif result and not result.get("generated"):
            logger.info("auth: bootstrapped admin %s from env/config", result["username"])
    except ValueError:
        raise
    except Exception:
        logger.error("auth bootstrap FAILED — no admin may exist", exc_info=True)
    return st


def _configs_dir() -> Path:
    return Path(
        os.getenv("REDIBIS_CONFIGS_DIR")
        or os.getenv("CONFIGS_DIR")
        or "./configs"
    )


def get_config_store():
    st = _stores.get("config")
    if st is not None:
        return st
    backend = get_backend_store()
    if isinstance(backend, LocalBackend):
        st = LocalConfigStore(_configs_dir())
    else:
        st = ObjectConfigStore(backend)
    _stores["config"] = st
    try:
        from redibis.store.config_bootstrap import ensure_bundled_configs

        ensure_bundled_configs(st)
    except Exception:
        logger.debug("bundled config seed skipped", exc_info=True)
    return st


_CONNECTION_ERRORS = (
    ConnectionError,
    TimeoutError,
    OSError,
)


def _is_connection_error(exc: BaseException) -> bool:
    if isinstance(exc, _CONNECTION_ERRORS):
        return True
    name = type(exc).__name__
    if name in ("ClientError", "EndpointConnectionError", "NoCredentialsError"):
        return True
    cause = exc.__cause__
    return cause is not None and _is_connection_error(cause)


def with_store_retry(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run *fn* once; on connection failure invalidate caches and retry once."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        if not _is_connection_error(exc):
            raise
        logger.warning("store connection error — rebuilding handle: %s", exc)
        clear_stores()
        return fn(*args, **kwargs)


def store_op(fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Hot-path store call with one reconnect retry."""
    return with_store_retry(fn, *args, **kwargs)


def load_global_settings() -> dict:
    return store_op(get_config_store().load_global_settings)


def backend_ping() -> bool:
    return bool(store_op(get_backend_store().ping))


def memory_ready() -> bool:
    """True when memory is disabled or the configured store is reachable."""
    try:
        store = get_contract_store()
        mc = store.memory_config
        if not mc.enabled:
            return True
        mem = getattr(store, "_memory_store", None)
        if mem is None:
            return False
        health = getattr(mem, "health", None)
        if callable(health):
            return bool(health())
        ping = getattr(mem, "ping", None)
        if callable(ping):
            return bool(ping())
        return True
    except Exception:
        return False
