"""
redibis.metadata.credentials
============================
Resolve JDBC credential references at runtime — never persist raw secrets.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from redibis.config import ConfigError


def resolve_credential_ref(ref: str) -> dict[str, Any]:
    """
    Resolve ``credential_ref`` to connection kwargs.

    Supported forms:
    - env var name → JSON object ``{"username": "...", "password": "..."}``
    - file path → same JSON shape
    """
    if not ref:
        raise ConfigError("jdbc.credential_ref is required for JDBC metadata retrieval")

    raw: str | None = None
    if ref in os.environ:
        raw = os.environ[ref]
    else:
        path = Path(ref)
        if path.is_file():
            raw = path.read_text(encoding="utf-8").strip()

    if not raw:
        raise ConfigError(
            f"credential_ref {ref!r} not found in environment or as a readable file"
        )

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"credential_ref {ref!r} must contain JSON credentials") from exc

    if not isinstance(data, dict):
        raise ConfigError(f"credential_ref {ref!r} must decode to a JSON object")

    return data
