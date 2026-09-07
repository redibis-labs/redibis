"""
Resolve memory-store DSN references at runtime (never persist raw secrets).
"""

from __future__ import annotations

import os
from pathlib import Path

from redibis.config import ConfigError


def resolve_dsn_ref(ref: str) -> str:
    if not ref:
        raise ConfigError("memory.dsn_ref is required when memory is enabled")

    if ref in os.environ:
        return os.environ[ref].strip()

    path = Path(ref)
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()

    raise ConfigError(
        f"dsn_ref {ref!r} not found in environment or as a readable file"
    )
