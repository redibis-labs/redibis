"""
Custom LLM provider profiles — CRUD, staged connectivity test, models proxy.

Profiles are persisted into the user-level ``./llm_providers.json`` (or
``REDIBIS_LLM_PROVIDERS``) with ``kind: "custom"``. Packaged defaults stay
read-only. All completion calls still go through LiteLLM via ``get_provider``.
"""

from __future__ import annotations

import json
import ipaddress
import logging
import os
import re
import socket
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, build_opener

from redibis.enrich.llm_logging import redact
from redibis.enrich.providers import (
    EnrichmentError,
    _DEFAULT_FILE,
    _normalize_endpoint_and_key,
    _read_providers,
    _validated_api_base,
    bare_litellm_model,
    get_provider,
    load_provider_configs,
    user_provider_config_path,
)

log = logging.getLogger(__name__)

PROFILE_NAME_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
RESERVED_PARAM_KEYS = frozenset({
    "model", "messages", "api_base", "api_key", "response_format",
})

# Sensible SGLang + local Qwen defaults for the new-profile form.
SGLANG_QWEN_PRESET: dict[str, Any] = {
    "name": "sglang-qwen",
    "description": "SGLang-hosted Qwen (local)",
    "profile_type": "openai_compatible",
    "litellm_model": "openai/Qwen/Qwen2.5-14B-Instruct",
    "model_prefix": "openai",
    "api_base": "http://localhost:30000/v1",
    "api_key_env": "SGLANG_API_KEY",
    "supports_json": True,
    "residency": "local",
    "params": {"temperature": 0.2, "max_tokens": 4096, "timeout": 120},
}

GENERIC_OPENAI_PRESET: dict[str, Any] = {
    "name": "",
    "description": "OpenAI-compatible local server",
    "profile_type": "openai_compatible",
    "litellm_model": "openai/",
    "model_prefix": "openai",
    "api_base": "",
    "api_key_env": "OPENAI_API_KEY",
    "supports_json": True,
    "residency": "local",
    "params": {"temperature": 0.2, "max_tokens": 4096, "timeout": 120},
}

_MODELS_TIMEOUT_S = 5.0
_MODELS_MAX_BYTES = 256 * 1024
_TEST_TIMEOUT_S = 20.0


def packaged_provider_names() -> set[str]:
    return set(_read_providers(_DEFAULT_FILE).keys())


def sanitize_params(params: Any) -> dict:
    """Validate params is a JSON object; strip reserved / controlled keys."""
    if params is None:
        return {}
    if not isinstance(params, dict):
        raise EnrichmentError("params must be a JSON object")
    out: dict = {}
    for k, v in params.items():
        key = str(k)
        if key in RESERVED_PARAM_KEYS:
            continue
        out[key] = v
    return out


def validate_profile_name(name: str) -> str:
    key = (name or "").strip().lower()
    if not PROFILE_NAME_RE.fullmatch(key):
        raise EnrichmentError(
            f"Invalid profile name {name!r}. Use 1–64 chars: a-z, 0-9, _, -."
        )
    return key


def _try_flock(fh, exclusive: bool = True) -> None:
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows
        return
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)


def _try_funlock(fh) -> None:
    try:
        import fcntl
    except ImportError:  # pragma: no cover
        return
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass


def _backup_and_write(path: Path, payload: dict) -> None:
    """Read-modify-write with .bak backup and atomic replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2) + "\n"
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as lock_fh:
        _try_flock(lock_fh, exclusive=True)
        try:
            if path.exists():
                bak = path.with_suffix(path.suffix + ".bak")
                shutil.copy2(path, bak)
            fd, tmp_name = tempfile.mkstemp(
                dir=str(path.parent), prefix=".llm_providers.", suffix=".tmp",
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                    tmp.write(text)
                    tmp.flush()
                    os.fsync(tmp.fileno())
                os.replace(tmp_name, path)
            except Exception:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
        finally:
            _try_funlock(lock_fh)


def _user_file_providers(config_path: Optional[str] = None) -> dict[str, dict]:
    path = Path(config_path) if config_path else user_provider_config_path()
    if not path.exists():
        return {}
    return _read_providers(path)


def _profile_source(
    name: str,
    cfg: dict,
    *,
    packaged: set[str],
    user_cfgs: dict[str, dict],
) -> tuple[str, bool]:
    """Return (source, editable)."""
    user_entry = user_cfgs.get(name)
    if user_entry and user_entry.get("kind") == "custom":
        return "custom", True
    if name in packaged and name not in user_cfgs:
        return "packaged", False
    if name in user_cfgs:
        return "file", False
    return "packaged", False


def list_profiles(config_path: Optional[str] = None) -> list[dict]:
    """Merged registry with source / editable flags (never returns key values)."""
    packaged = packaged_provider_names()
    user_cfgs = _user_file_providers(config_path)
    cfgs = load_provider_configs(config_path)
    out: list[dict] = []
    for name, cfg in sorted(cfgs.items()):
        kind = cfg.get("kind", "")
        api_key_env = cfg.get("api_key_env") or ""
        litellm_model = cfg.get("litellm_model", "") or ("offline" if kind == "demo" else "")
        model_prefix = cfg.get("model_prefix") or ""
        known_models = list(cfg.get("known_models") or [])
        default_bare = bare_litellm_model(litellm_model, model_prefix=model_prefix)
        if default_bare and default_bare not in known_models:
            known_models.insert(0, default_bare)
        source, editable = _profile_source(
            name, cfg, packaged=packaged, user_cfgs=user_cfgs,
        )
        env_set = bool(api_key_env and os.getenv(api_key_env))
        if name == "gemini" and not env_set:
            env_set = bool(os.getenv("GOOGLE_API_KEY"))
        out.append({
            "name": name,
            "description": cfg.get("description", ""),
            "model": litellm_model,
            "model_prefix": model_prefix,
            "default_model_bare": default_bare,
            "known_models": known_models,
            "needs_key": False if kind == "demo" else bool(api_key_env or cfg.get("api_key")),
            "api_key_env": api_key_env,
            "api_key_env_set": env_set,
            "api_key_saved": False,  # custom profiles never persist keys
            "api_base": cfg.get("api_base"),
            "residency": cfg.get("residency") or "",
            "kind": kind or "litellm",
            "offline": kind == "demo",
            "cloud": name in ("claude", "gemini", "openai", "openrouter", "google_genai"),
            "supports_json": bool(cfg.get("supports_json", True)) if kind != "demo" else False,
            "params": dict(cfg.get("params") or {}) if isinstance(cfg.get("params"), dict) else {},
            "source": source,
            "editable": editable,
        })
    order = {
        "google_genai": 0, "gemini": 1, "ollama": 2, "vllm": 3, "sglang": 4,
        "openai": 5, "claude": 6, "openrouter": 7, "demo": 99,
    }
    out.sort(key=lambda row: (
        0 if row.get("editable") else 1,
        order.get(row["name"], 50),
        row["name"],
    ))
    return out


def _build_litellm_model(
    *,
    profile_type: str,
    model: str,
    litellm_model: str,
    model_prefix: str,
) -> tuple[str, str]:
    """Return (litellm_model, model_prefix)."""
    ptype = (profile_type or "openai_compatible").strip().lower()
    bare = (model or "").strip()
    raw = (litellm_model or "").strip()
    prefix = (model_prefix or "").strip()

    if ptype in ("raw", "raw_litellm", "litellm"):
        if not raw and bare:
            raw = bare
        if not raw:
            raise EnrichmentError("raw LiteLLM profile requires litellm_model or model")
        # Keep whatever prefix the user wrote (or infer from first segment)
        if "/" in raw and not prefix:
            prefix = raw.split("/", 1)[0]
        return raw, prefix or ""

    # openai_compatible (default)
    prefix = prefix or "openai"
    if bare:
        return f"{prefix}/{bare}", prefix
    if raw:
        if not raw.startswith(prefix + "/"):
            # Allow full openai/… strings
            if "/" in raw:
                return raw, prefix
            return f"{prefix}/{raw}", prefix
        return raw, prefix
    raise EnrichmentError("OpenAI-compatible profile requires a model id")


def normalize_profile_payload(payload: dict, *, name: Optional[str] = None) -> dict:
    """Validate and normalize a create/update or inline-test payload."""
    key = validate_profile_name(name or payload.get("name") or "")
    description = str(payload.get("description") or "").strip()
    profile_type = str(
        payload.get("profile_type") or payload.get("type") or "openai_compatible"
    ).strip().lower()
    if profile_type in ("openai", "openai-compatible", "openai_compat"):
        profile_type = "openai_compatible"
    if profile_type in ("raw_litellm", "litellm_string"):
        profile_type = "raw"

    api_base_raw = payload.get("api_base") or payload.get("endpoint_url")
    api_base = None
    if api_base_raw is not None and str(api_base_raw).strip():
        api_base = _validated_api_base(str(api_base_raw), provider_name=key)

    model = str(payload.get("model") or payload.get("default_model_bare") or "").strip()
    litellm_in = str(payload.get("litellm_model") or "").strip()
    model_prefix = str(payload.get("model_prefix") or "").strip()
    litellm_model, model_prefix = _build_litellm_model(
        profile_type=profile_type,
        model=model,
        litellm_model=litellm_in,
        model_prefix=model_prefix,
    )

    api_key_env = str(payload.get("api_key_env") or "").strip()
    supports_json = bool(payload.get("supports_json", True))
    residency = str(payload.get("residency") or "local").strip() or "local"
    params = sanitize_params(payload.get("params"))

    if "api_key" in payload and payload.get("api_key"):
        raise EnrichmentError(
            "Literal api_key must not be stored in profiles. "
            "Use api_key_env and pass a session key only for Test / enrich."
        )

    known = payload.get("known_models")
    known_models: list[str] = []
    if isinstance(known, list):
        known_models = [str(m).strip() for m in known if str(m).strip()]
    bare = bare_litellm_model(litellm_model, model_prefix=model_prefix)
    if bare and bare not in known_models:
        known_models.insert(0, bare)

    entry = {
        "kind": "custom",
        "description": description or f"Custom provider {key}",
        "litellm_model": litellm_model,
        "model_prefix": model_prefix,
        "supports_json": supports_json,
        "residency": residency,
        "params": params,
        "profile_type": profile_type,
        "known_models": known_models,
    }
    if api_base:
        entry["api_base"] = api_base
    if api_key_env:
        entry["api_key_env"] = api_key_env
    return {"name": key, "entry": entry}


def save_profile(
    payload: dict,
    *,
    allow_override_packaged: bool = False,
    config_path: Optional[str] = None,
) -> dict:
    """Create or update a ``kind=custom`` profile in the user registry."""
    normalized = normalize_profile_payload(payload)
    name = normalized["name"]
    entry = normalized["entry"]
    path = Path(config_path) if config_path else user_provider_config_path()

    if name in packaged_provider_names() and not allow_override_packaged:
        raise EnrichmentError(
            f"Name {name!r} collides with a packaged provider. "
            "Choose a different name, or pass allow_override_packaged=true."
        )

    existing = _read_providers(path) if path.exists() else {}
    prior = existing.get(name)
    if prior and prior.get("kind") not in (None, "", "custom") and name in packaged_provider_names():
        # Overwriting a non-custom packaged override still requires the flag
        if not allow_override_packaged:
            raise EnrichmentError(
                f"Provider {name!r} already exists and is not a custom profile."
            )
    if prior and prior.get("kind") not in (None, "", "custom") and name not in packaged_provider_names():
        if prior.get("kind") != "custom":
            raise EnrichmentError(
                f"Provider {name!r} exists with kind={prior.get('kind')!r}; "
                "UI may only edit kind=custom profiles."
            )

    # Preserve unknown fields from a previous custom entry
    merged = dict(prior) if isinstance(prior, dict) and prior.get("kind") == "custom" else {}
    merged.update(entry)
    merged.pop("api_key", None)  # never persist keys
    existing[name] = merged

    # Preserve top-level comment/_meta from existing file when present
    payload_out: dict[str, Any] = {"providers": existing}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for k, v in raw.items():
                    if k != "providers" and str(k).startswith("_"):
                        payload_out[k] = v
        except Exception:
            pass

    _backup_and_write(path, payload_out)
    return {
        "ok": True,
        "name": name,
        "path": str(path.resolve()),
        "profile": {k: v for k, v in merged.items() if k != "api_key"},
    }


def delete_profile(name: str, *, config_path: Optional[str] = None) -> dict:
    """Delete a custom profile only."""
    key = validate_profile_name(name)
    path = Path(config_path) if config_path else user_provider_config_path()
    existing = _read_providers(path) if path.exists() else {}
    entry = existing.get(key)
    if entry is None:
        raise EnrichmentError(f"Custom profile {key!r} not found in {path}")
    if entry.get("kind") != "custom":
        raise EnrichmentError(
            f"Provider {key!r} is not a custom profile (kind={entry.get('kind')!r}); "
            "cannot delete packaged/file providers from the UI."
        )
    del existing[key]
    payload_out: dict[str, Any] = {"providers": existing}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for k, v in raw.items():
                    if k != "providers" and str(k).startswith("_"):
                        payload_out[k] = v
        except Exception:
            pass
    _backup_and_write(path, payload_out)
    return {"ok": True, "name": key, "path": str(path.resolve())}


# ── Models proxy + staged test ───────────────────────────────────────────────


def _models_fetch_allow_private(cfg: dict, api_base: str = "") -> bool:
    """Local / unspecified residency may target loopback and RFC1918 hosts.

    Cloud residencies stay fail-closed. Link-local / metadata addresses remain
    blocked inside ``_assert_safe_remote_host`` even when this returns True.
    """
    residency = str((cfg or {}).get("residency") or "local").strip().lower()
    if residency in {"public", "gcp", "cloud", "external"}:
        return False
    return True


def _assert_http_url(api_base: str) -> str:
    validated = _validated_api_base(api_base, provider_name="custom")
    if not validated:
        raise EnrichmentError("api_base is required")
    return validated.rstrip("/")


def _models_url(api_base: str) -> str:
    base = _assert_http_url(api_base)
    if base.endswith("/models"):
        return base
    return f"{base}/models"


def _host_of(url: str) -> str:
    return (urlparse(url).netloc or "").lower()


def _assert_safe_remote_host(url: str, *, allow_private: bool) -> None:
    host = (urlparse(url).hostname or "").strip()
    if not host:
        raise EnrichmentError("api_base host is required")
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        }
    except OSError as exc:
        raise EnrichmentError(f"Could not resolve provider host {host!r}") from exc
    if not addresses:
        raise EnrichmentError(f"Could not resolve provider host {host!r}")
    for raw in addresses:
        address = ipaddress.ip_address(raw)
        always_blocked = (
            address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        )
        private_target = address.is_private or address.is_loopback
        if always_blocked or (private_target and not allow_private):
            raise EnrichmentError(
                f"Provider host {host!r} resolves to blocked address {address}"
            )


def fetch_remote_models(
    api_base: str,
    *,
    session_key: Optional[str] = None,
    timeout: float = _MODELS_TIMEOUT_S,
    allow_private: bool = False,
) -> dict[str, Any]:
    """
    GET {api_base}/models with SSRF guards.

    Only calls the validated api_base host; rejects cross-host redirects;
    caps response size; short timeout. Never logs the session key.
    """
    url = _models_url(api_base)
    _assert_safe_remote_host(url, allow_private=allow_private)
    origin_host = _host_of(url)
    headers = {"Accept": "application/json"}
    key = (session_key or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"

    from urllib.request import HTTPRedirectHandler

    class _GuardHandler(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: A002
            new_host = _host_of(newurl)
            if new_host and new_host != origin_host:
                raise HTTPError(
                    newurl, code, f"cross-host redirect blocked to {new_host}", headers, fp,
                )
            return None

    opener = build_opener(_GuardHandler)
    req = Request(url, headers=headers, method="GET")
    t0 = time.perf_counter()
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read(_MODELS_MAX_BYTES + 1)
            status = getattr(resp, "status", None) or resp.getcode()
    except HTTPError as exc:
        body = ""
        try:
            body = exc.read(_MODELS_MAX_BYTES).decode("utf-8", errors="replace")
        except Exception:
            body = str(exc)
        return {
            "ok": False,
            "error": redact(
                f"HTTP {exc.code}: {body[:500] or exc.reason}",
                secrets=[key] if key else [],
            ),
            "models": [],
            "latency_ms": round((time.perf_counter() - t0) * 1000),
            "url": url,
            "status": exc.code,
        }
    except URLError as exc:
        return {
            "ok": False,
            "error": redact(f"URLError: {exc.reason}", secrets=[key] if key else []),
            "models": [],
            "latency_ms": round((time.perf_counter() - t0) * 1000),
            "url": url,
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": redact(f"{type(exc).__name__}: {exc}", secrets=[key] if key else []),
            "models": [],
            "latency_ms": round((time.perf_counter() - t0) * 1000),
            "url": url,
        }

    if len(raw) > _MODELS_MAX_BYTES:
        return {
            "ok": False,
            "error": f"models response exceeded {_MODELS_MAX_BYTES} bytes",
            "models": [],
            "latency_ms": round((time.perf_counter() - t0) * 1000),
            "url": url,
        }

    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception as exc:
        return {
            "ok": False,
            "error": f"invalid JSON from /models: {exc}",
            "models": [],
            "latency_ms": round((time.perf_counter() - t0) * 1000),
            "url": url,
            "status": status,
        }

    ids: list[str] = []
    items = data.get("data") if isinstance(data, dict) else None
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict) and item.get("id"):
                ids.append(str(item["id"]))
            elif isinstance(item, str):
                ids.append(item)
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item.get("id"):
                ids.append(str(item["id"]))
            elif isinstance(item, str):
                ids.append(item)

    return {
        "ok": True,
        "models": ids,
        "latency_ms": round((time.perf_counter() - t0) * 1000),
        "url": url,
        "status": status,
    }


def _resolve_test_cfg(
    name_or_payload: Any,
    *,
    config_path: Optional[str] = None,
) -> tuple[str, dict, Optional[str], Optional[str]]:
    """
    Return (name, cfg_dict, model_override, endpoint_override).

    Accepts a saved name (str) or an unsaved payload dict.
    """
    if isinstance(name_or_payload, str):
        key = (name_or_payload or "").strip().lower()
        cfgs = load_provider_configs(config_path)
        cfg = cfgs.get(key)
        if cfg is None:
            raise EnrichmentError(f"Unknown provider {key!r}")
        return key, dict(cfg), None, None

    if not isinstance(name_or_payload, dict):
        raise EnrichmentError("test payload must be a name string or a profile object")

    # Unsaved payload — normalize without writing
    if name_or_payload.get("name") or name_or_payload.get("api_base") or name_or_payload.get("litellm_model"):
        normalized = normalize_profile_payload(
            name_or_payload,
            name=name_or_payload.get("name") or "inline-test",
        )
        name = normalized["name"]
        entry = normalized["entry"]
        model_override = str(
            name_or_payload.get("model")
            or bare_litellm_model(entry["litellm_model"], model_prefix=entry.get("model_prefix") or "")
            or ""
        ).strip() or None
        endpoint = entry.get("api_base")
        return name, entry, model_override, endpoint

    raise EnrichmentError("test payload missing name / api_base / model")


def _completion_probe(
    *,
    name: str,
    cfg: dict,
    model: Optional[str],
    endpoint: Optional[str],
    session_key: Optional[str],
    json_mode: bool,
    timeout: float,
    config_path: Optional[str],
) -> dict[str, Any]:
    """Run a tiny LiteLLM completion against a (possibly unsaved) config."""
    secrets = [session_key] if session_key else []
    t0 = time.perf_counter()
    timeout = max(1.0, min(float(timeout or 20.0), 60.0))

    # For unsaved configs, temporarily inject into a temp providers file OR
    # build LiteLLMProvider directly.
    from redibis.enrich.providers import LiteLLMProvider, _resolve_key

    if cfg.get("kind") == "demo" or name == "demo":
        provider = get_provider("demo", config_path=config_path)
    else:
        params = sanitize_params(cfg.get("params"))
        params.setdefault("timeout", timeout)
        params.setdefault("max_tokens", 16)
        api_base, resolved_key = _normalize_endpoint_and_key(
            endpoint, session_key, registry_api_base=cfg.get("api_base"),
        )
        provider = LiteLLMProvider(
            name=name,
            model=model or "",
            litellm_model=cfg.get("litellm_model") or "",
            model_prefix=cfg.get("model_prefix") or "",
            api_base=api_base,
            api_key=_resolve_key(cfg, resolved_key or session_key, provider_name=name),
            supports_json=bool(cfg.get("supports_json", True)),
            residency=str(cfg.get("residency") or ""),
            extra=params,
        )

    try:
        from redibis.config import RedibisConfig
        from redibis.telemetry.model_gateway import guarded_model_call

        prompt = 'Reply with exactly: {"ok": true}' if json_mode else "Reply with exactly: OK"
        cfg_path = os.environ.get("REDIBIS_CONFIG")
        redibis_config = (
            RedibisConfig.from_yaml(cfg_path) if cfg_path else RedibisConfig.default()
        )

        def _invoke() -> str:
            return provider.complete(
                "You are a connectivity probe.",
                prompt,
                json_mode=json_mode,
            )

        out, _rai = guarded_model_call(
            _invoke,
            model_id=str(
                getattr(provider, "_effective_model", lambda: "")()
                or getattr(provider, "model", "")
                or name
            ),
            provider=provider,
            user_prompt=prompt,
            system_prompt="You are a connectivity probe.",
            redibis_config=redibis_config,
            model_role="provider.probe",
        )
        return {
            "ok": True,
            "error": "",
            "response_snippet": (out or "")[:500],
            "latency_ms": round((time.perf_counter() - t0) * 1000),
            "model": getattr(provider, "_effective_model", lambda: "")(),
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": redact(format_provider_error(exc), secrets=secrets),
            "response_snippet": "",
            "latency_ms": round((time.perf_counter() - t0) * 1000),
            "model": model or cfg.get("litellm_model") or "",
        }


def format_provider_error(exc: BaseException, *, max_body: int = 2000) -> str:
    """Capture LiteLLM exception class, HTTP status, and truncated response body."""
    parts = [f"{type(exc).__name__}: {exc}"]
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(exc, "code", None)
    if status is not None:
        parts.append(f"status={status}")

    body = None
    for attr in ("response", "body", "message"):
        val = getattr(exc, attr, None)
        if val is None:
            continue
        try:
            if hasattr(val, "text"):
                body = val.text
            elif hasattr(val, "content"):
                content = val.content
                body = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else str(content)
            elif isinstance(val, (bytes, bytearray)):
                body = bytes(val).decode("utf-8", errors="replace")
            elif isinstance(val, dict):
                body = json.dumps(val)
            elif attr == "message" and val is exc:
                continue
            else:
                text = str(val)
                if text and text != str(exc):
                    body = text
        except Exception:
            continue
        if body:
            break

    if body:
        parts.append(f"body={str(body)[:max_body]}")
    return redact(" | ".join(parts))


def test_profile(
    name_or_payload: Any,
    *,
    session_key: Optional[str] = None,
    config_path: Optional[str] = None,
    timeout: float = _TEST_TIMEOUT_S,
) -> dict[str, Any]:
    """
    Three-stage connectivity test.

    1. GET {api_base}/models (reachability + served model list)
    2. 1-token completion via LiteLLM
    3. completion with response_format=json_object (if supports_json)

    Accepts a saved name or an unsaved payload. Session key is never persisted.
    """
    secrets = [session_key] if session_key else []
    try:
        name, cfg, model_override, endpoint_override = _resolve_test_cfg(
            name_or_payload, config_path=config_path,
        )
    except Exception as exc:
        return {
            "ok": False,
            "provider": "",
            "stages": [{
                "stage": "config",
                "ok": False,
                "error": redact(str(exc), secrets=secrets),
            }],
            "error": redact(str(exc), secrets=secrets),
            "hint": "",
        }

    stages: list[dict] = []
    api_base = endpoint_override or cfg.get("api_base") or ""
    model = model_override
    if not model:
        model = bare_litellm_model(
            cfg.get("litellm_model") or "",
            model_prefix=cfg.get("model_prefix") or "",
        ) or None

    # Stage 1 — models endpoint (skip for demo / no api_base)
    if cfg.get("kind") == "demo" or name == "demo":
        stages.append({
            "stage": "reachability",
            "ok": True,
            "error": "",
            "detail": "demo provider (offline — skipped HTTP /models)",
            "models": [],
        })
    elif not api_base:
        stages.append({
            "stage": "reachability",
            "ok": True,
            "error": "",
            "detail": "no api_base — skipped /models (cloud/hosted provider)",
            "models": [],
        })
    else:
        models_result = fetch_remote_models(
            api_base,
            session_key=session_key,
            timeout=min(timeout, _MODELS_TIMEOUT_S),
            allow_private=_models_fetch_allow_private(cfg, api_base),
        )
        stage1 = {
            "stage": "reachability",
            "ok": bool(models_result.get("ok")),
            "error": models_result.get("error") or "",
            "models": models_result.get("models") or [],
            "latency_ms": models_result.get("latency_ms"),
            "url": models_result.get("url"),
        }
        if stage1["ok"] and model and stage1["models"] and model not in stage1["models"]:
            stage1["warning"] = (
                f"Model {model!r} not in served list {stage1['models'][:8]}. "
                "Use the exact id from GET /v1/models (or --served-model-name)."
            )
        elif not stage1["ok"]:
            stage1["warning"] = (
                "GET /v1/models failed; continuing with a chat completion "
                "(enrich does not require the models endpoint)."
            )
        stages.append(stage1)

    # Stage 2 — plain completion
    stage2 = _completion_probe(
        name=name,
        cfg=cfg,
        model=model,
        endpoint=api_base or None,
        session_key=session_key,
        json_mode=False,
        timeout=timeout,
        config_path=config_path,
    )
    stages.append({
        "stage": "completion",
        "ok": stage2["ok"],
        "error": redact(stage2.get("error") or "", secrets=secrets),
        "response_snippet": stage2.get("response_snippet") or "",
        "latency_ms": stage2.get("latency_ms"),
        "model": stage2.get("model") or model,
    })
    if not stage2["ok"]:
        hint = (
            "Auth or model-name check failed. Local servers often still need a "
            "non-empty key (e.g. sk-local). Confirm the model id matches GET /v1/models."
        )
        return {
            "ok": False,
            "provider": name,
            "stages": stages,
            "error": redact(stage2.get("error") or "", secrets=secrets),
            "hint": hint,
        }

    # Stage 3 — JSON mode (optional)
    if bool(cfg.get("supports_json", True)) and cfg.get("kind") != "demo":
        stage3 = _completion_probe(
            name=name,
            cfg=cfg,
            model=model,
            endpoint=api_base or None,
            session_key=session_key,
            json_mode=True,
            timeout=timeout,
            config_path=config_path,
        )
        stages.append({
            "stage": "json_mode",
            "ok": stage3["ok"],
            "error": redact(stage3.get("error") or "", secrets=secrets),
            "response_snippet": stage3.get("response_snippet") or "",
            "latency_ms": stage3.get("latency_ms"),
            "model": stage3.get("model") or model,
        })
        if not stage3["ok"]:
            return {
                "ok": False,
                "provider": name,
                "stages": stages,
                "error": redact(stage3.get("error") or "", secrets=secrets),
                "hint": (
                    "Server rejected response_format=json_object. "
                    "Toggle supports_json off — enrichment will still ask for JSON "
                    "in the prompt and parse the reply."
                ),
            }
    else:
        stages.append({
            "stage": "json_mode",
            "ok": True,
            "error": "",
            "detail": "skipped (supports_json=false or demo)",
        })

    return {
        "ok": True,
        "provider": name,
        "stages": stages,
        "error": "",
        "hint": "",
        "models": next(
            (s.get("models") for s in stages if s.get("stage") == "reachability"),
            [],
        ),
    }
