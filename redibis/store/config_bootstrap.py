"""Seed packaged default named configs into the config store when missing."""

from __future__ import annotations

import logging
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

_BUNDLED_ROOT = "redibis.store.bundled_configs"


def ensure_bundled_configs(store: Any) -> dict[str, list[str]]:
    """
    Install packaged regex + quality configs when absent.

    Safe to call on every startup — never overwrites operator saves.
    """
    from redibis.pii.regex_overrides import RegexOverrides

    seeded: dict[str, list[str]] = {"regex": [], "quality": []}

    try:
        regex_names = {m["name"] for m in store.list_regex_configs()}
    except Exception:
        regex_names = set()
    try:
        quality_names = {m["name"] for m in store.list_quality_configs()}
    except Exception:
        quality_names = set()

    regex_dir = resources.files(_BUNDLED_ROOT) / "regex"
    for entry in sorted(regex_dir.iterdir(), key=lambda p: p.name):
        if entry.name.endswith(".yaml"):
            data = yaml.safe_load(entry.read_text(encoding="utf-8")) or {}
            name = str(data.get("name") or Path(entry.name).stem)
            if name in regex_names:
                continue
            overrides = RegexOverrides.from_dict(data.get("config", {}))
            store.save_regex_config(name, overrides, str(data.get("description") or ""))
            seeded["regex"].append(name)
            log.info("Seeded bundled regex config %r", name)

    quality_dir = resources.files(_BUNDLED_ROOT) / "quality"
    for entry in sorted(quality_dir.iterdir(), key=lambda p: p.name):
        if entry.name.endswith(".yaml"):
            data = yaml.safe_load(entry.read_text(encoding="utf-8")) or {}
            name = str(data.get("name") or Path(entry.name).stem)
            if name in quality_names:
                continue
            rules = data.get("rules") or []
            store.save_quality_config(
                name,
                rules,
                str(data.get("description") or ""),
                ge_code=str(data.get("ge_code") or ""),
            )
            seeded["quality"].append(name)
            log.info("Seeded bundled quality config %r", name)

    return seeded
