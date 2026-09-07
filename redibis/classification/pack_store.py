"""
redibis.classification.pack_store
=================================
Pack library for the agent composer: list / read / save classification policy packs.

Built-in packs ship in ``redibis.classification.packs``; user packs live in a writable
directory (``REDIBIS_PACK_DIR`` env, else ``./redibis_packs``). Saving validates the YAML
through ``_parse_pack`` before writing, so an invalid pack never lands.
"""

from __future__ import annotations

import importlib.resources
import os
import re
from pathlib import Path

import yaml

from redibis.classification.policy_pack import (
    ClassificationPolicy,
    _PACK_CACHE,
    _parse_pack,
    get_builtin_pack,
    list_builtin_packs,
    load_policy_pack,
)
from redibis.config import ConfigError

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def user_pack_dir() -> Path:
    return Path(os.environ.get("REDIBIS_PACK_DIR") or "./redibis_packs")


def _safe_name(name: str) -> str:
    n = (name or "").strip().lower()
    if not _NAME_RE.match(n):
        raise ConfigError(f"invalid pack name {name!r} (use a-z, 0-9, _-, ≤64 chars)")
    return n


def list_packs() -> list[str]:
    """Built-in + user pack names (deduped, sorted)."""
    names = set(list_builtin_packs())
    d = user_pack_dir()
    if d.is_dir():
        names |= {p.stem for p in d.iterdir() if p.suffix in (".yaml", ".yml")}
    return sorted(names)


def get_pack_text(name: str) -> str:
    """Raw YAML for a pack — user copy wins over the built-in."""
    n = _safe_name(name)
    user = user_pack_dir() / f"{n}.yaml"
    if user.is_file():
        return user.read_text(encoding="utf-8")
    pkg = importlib.resources.files("redibis.classification.packs")
    for ext in ("yaml", "yml"):
        cand = pkg / f"{n}.{ext}"
        if cand.is_file():
            return cand.read_text(encoding="utf-8")
    raise ConfigError(f"pack {name!r} not found")


def is_user_pack(name: str) -> bool:
    """True when a writable user copy exists for this pack name."""
    n = _safe_name(name)
    return (user_pack_dir() / f"{n}.yaml").is_file()


def is_builtin_pack(name: str) -> bool:
    return _safe_name(name) in list_builtin_packs()


def load_pack(name: str) -> ClassificationPolicy:
    """Load a pack by name — user copy wins over the packaged built-in."""
    n = _safe_name(name)
    user = user_pack_dir() / f"{n}.yaml"
    if user.is_file():
        return load_policy_pack(user)
    return get_builtin_pack(n)


def save_pack_text(name: str, text: str) -> str:
    """Validate then write a user pack. Returns its resolved path."""
    n = _safe_name(name)
    raw = yaml.safe_load(text) or {}
    _parse_pack(raw)  # raises ConfigError on an invalid pack
    d = user_pack_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{n}.yaml"
    path.write_text(text, encoding="utf-8")
    _PACK_CACHE.pop(n, None)  # force reload on next get_builtin_pack/load
    return str(path.resolve())
