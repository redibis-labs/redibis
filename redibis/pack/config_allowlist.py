"""Allow-list for ``config/redibis.yaml`` inside a Redibis Pack."""

from __future__ import annotations

import copy
from typing import Any, Mapping

from redibis.pack.errors import PackValidationError

# Portable behavior knobs only — secrets and deployment endpoints stay local.
ALLOWED_CONFIG_TOP_LEVEL = frozenset(
    {
        "scan_types",
        "profiling",
        "quality",
        "pii",
        "contract",
        "masking",
        "classification",
        "behavior",
        "report",
    }
)

# Explicit denylist for nested secret / deployment keys (path-specific errors).
FORBIDDEN_CONFIG_PATHS = frozenset(
    {
        "storage",
        "source",
        "llm",
        "catalog",
        "table",
        "agents",
        "memory",
        "rai",
        "packs",
        "pack",
        "masking.keys",
        "masking.seed",
        "masking.run_keys",
        "masking.key",
        "pii.llm",
    }
)


def _walk_forbidden(node: Any, prefix: str, errors: list[str]) -> None:
    if not isinstance(node, dict):
        return
    for key, value in node.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if path in FORBIDDEN_CONFIG_PATHS or str(key) in FORBIDDEN_CONFIG_PATHS:
            errors.append(f"excluded config key not allowed in pack: {path}")
            continue
        _walk_forbidden(value, path, errors)


def _delete_path(tree: dict[str, Any], dotted: str) -> None:
    parts = dotted.split(".")
    cur: Any = tree
    for part in parts[:-1]:
        if not isinstance(cur, dict) or part not in cur:
            return
        cur = cur[part]
    if isinstance(cur, dict):
        cur.pop(parts[-1], None)


def extract_portable_config(config: Any) -> dict[str, Any]:
    """Project a ``RedibisConfig`` (or dict) onto the pack allow-list.

    Excluded keys are stripped on export (never written). Import still
    rejects them loudly via :func:`validate_pack_config`.
    """
    if hasattr(config, "to_dict"):
        raw = config.to_dict()
    elif isinstance(config, Mapping):
        raw = dict(config)
    else:
        raise TypeError(f"expected RedibisConfig or mapping, got {type(config)!r}")
    if not isinstance(raw, dict):
        raise TypeError("config.to_dict() must return a mapping")

    out: dict[str, Any] = {
        key: copy.deepcopy(value)
        for key, value in raw.items()
        if key in ALLOWED_CONFIG_TOP_LEVEL
    }
    for path in sorted(FORBIDDEN_CONFIG_PATHS):
        if "." in path:
            _delete_path(out, path)
        elif path in out:
            del out[path]
    validate_pack_config(out)
    return out


def validate_pack_config(payload: Any) -> None:
    """Reject excluded keys loudly (never silently drop)."""
    if payload is None:
        return
    if not isinstance(payload, dict):
        raise PackValidationError(
            "config/redibis.yaml must be a mapping",
            errors=["config/redibis.yaml must be a mapping"],
        )
    errors: list[str] = []
    for key in payload:
        if key not in ALLOWED_CONFIG_TOP_LEVEL:
            errors.append(f"excluded config key not allowed in pack: {key}")
    _walk_forbidden(payload, "", errors)
    # Nested walk may duplicate top-level; unique while preserving order.
    uniq: list[str] = []
    seen: set[str] = set()
    for err in errors:
        if err not in seen:
            seen.add(err)
            uniq.append(err)
    if uniq:
        raise PackValidationError(
            "pack config contains excluded keys",
            errors=uniq,
        )
