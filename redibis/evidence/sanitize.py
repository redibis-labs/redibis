"""Strip credentials and auth material from config snapshots."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

_SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|auth(?:orization)?|passwd|password|secret|token|credential|"
    r"private[_-]?key|access[_-]?key|session[_-]?key|bearer)$",
    re.I,
)
_SECRET_VALUE_RE = re.compile(
    r"^(sk-|sk-ant-|xai-|gsk-|AIza)[A-Za-z0-9_\-]{8,}$"
)


def is_secret_key(key: str) -> bool:
    leaf = str(key or "").rsplit(".", 1)[-1].strip()
    return bool(_SECRET_KEY_RE.search(leaf.replace("-", "_")))


def sanitize_mapping(obj: Any) -> Any:
    """Recursively drop secret keys and redact key-like values. Never raises."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for key, value in obj.items():
            if is_secret_key(str(key)):
                continue
            out[str(key)] = sanitize_mapping(value)
        return out
    if isinstance(obj, (list, tuple)):
        return [sanitize_mapping(v) for v in obj]
    if isinstance(obj, str) and _SECRET_VALUE_RE.match(obj.strip()):
        return "[REDACTED]"
    if hasattr(obj, "__fspath__"):
        return str(obj)
    if hasattr(obj, "__dataclass_fields__"):
        return sanitize_mapping({
            f: getattr(obj, f) for f in obj.__dataclass_fields__
        })
    if isinstance(obj, (bool, int, float)) or obj is None:
        return obj
    try:
        json.dumps(obj)
        return obj
    except TypeError:
        return str(obj)


def config_sha256(obj: Any) -> str:
    payload = sanitize_mapping(obj)
    raw = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
