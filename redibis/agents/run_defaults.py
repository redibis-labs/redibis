"""Editable run-defaults pack for the agent composer (PII, sampling, planner)."""

from __future__ import annotations

import importlib.resources
import os
from pathlib import Path
from typing import Any

import yaml

from redibis.config import ConfigError

_NAME_RE = __import__("re").compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def defaults_dir() -> Path:
    return Path(os.environ.get("REDIBIS_AGENT_DEFAULTS_DIR") or "./redibis_agent_defaults")


def _safe_name(name: str) -> str:
    n = (name or "defaults").strip().lower()
    if not _NAME_RE.match(n):
        raise ConfigError(f"invalid defaults pack name {name!r}")
    return n


def _builtin_text() -> str:
    pkg = importlib.resources.files("redibis.agents.packs")
    return (pkg / "defaults.yaml").read_text(encoding="utf-8")


def get_defaults_text(name: str = "defaults") -> str:
    n = _safe_name(name)
    user = defaults_dir() / f"{n}.yaml"
    if user.is_file():
        return user.read_text(encoding="utf-8")
    if n == "defaults":
        return _builtin_text()
    raise ConfigError(f"defaults pack {name!r} not found")


def load_defaults(name: str = "defaults") -> dict[str, Any]:
    raw = yaml.safe_load(get_defaults_text(name)) or {}
    if not isinstance(raw, dict):
        raise ConfigError("defaults pack must be a YAML mapping")
    return raw


def save_defaults_text(text: str, name: str = "defaults") -> str:
    n = _safe_name(name)
    raw = yaml.safe_load(text) or {}
    if not isinstance(raw, dict):
        raise ConfigError("defaults pack must be a YAML mapping")
    d = defaults_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{n}.yaml"
    path.write_text(text, encoding="utf-8")
    return str(path.resolve())


def merge_defaults_into_params(params: dict[str, Any], defaults: dict[str, Any] | None = None) -> dict[str, Any]:
    """Fill missing node params from the defaults pack (does not override explicit values)."""
    d = defaults or load_defaults()
    out = dict(params or {})
    pii = d.get("pii") or {}
    quality = d.get("quality") or {}
    planner = d.get("planner") or {}
    profile = d.get("profile") or {}

    out.setdefault("pii_engines", pii.get("engines", "both"))
    out.setdefault("equation_mode", pii.get("equation_mode", "balanced"))
    out.setdefault("pii_sample_rows", pii.get("sample_rows", 5000))
    out.setdefault("quality_sample_rows", quality.get("sample_rows", 10000))
    out.setdefault("quality_strategy", quality.get("strategy", "fixed_rows"))
    out.setdefault("quality_fixed_row_count", quality.get("fixed_row_count", 10000))
    out.setdefault("quality_sample_fraction", quality.get("sample_fraction", 0.1))
    out.setdefault("sample_rows", pii.get("sample_rows", 5000))
    out.setdefault("provider", planner.get("provider") or "")
    out.setdefault("model", planner.get("model") or "")
    out.setdefault("profiler_engine", profile.get("engine", "great_expectations"))
    return out
