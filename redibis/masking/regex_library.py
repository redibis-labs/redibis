"""
redibis.masking.regex_library — registered regex pattern library for fake data.

A ``MaskingPlan`` column can use ``strategy: fake`` with ``kind: regex`` and
either a ``regex_pattern`` (inline) or a ``regex_library`` name that is resolved
here.

Resolution chain (mirrors ``masking_roles``):
    1. Explicit dict passed in by the caller
    2. ``REDIBIS_REGEX_PATTERNS`` env var → path to a JSON file
    3. ``./regex_patterns.json`` in the current working directory
    4. Packaged default (``redibis/masking/regex_patterns.json``)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional, Union

_PACKAGED = Path(__file__).with_name("regex_patterns.json")

_cache: Optional[dict] = None


# ─────────────────────────────────────────────────────────────────────────────
# Load / resolve
# ─────────────────────────────────────────────────────────────────────────────

def load_regex_library(source: Union[dict, str, Path, None] = None) -> dict:
    """Load and return the regex pattern library.

    Parameters
    ----------
    source : dict | str | Path | None
        If a *dict*, returned as-is.  If a *str* / *Path*, treated as a file
        path.  If *None*, the resolution chain is used.
    """
    global _cache

    if isinstance(source, dict):
        return source

    if isinstance(source, (str, Path)):
        path = Path(source)
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))

    if _cache is not None:
        return _cache

    # Resolution chain -------------------------------------------------------

    env = os.environ.get("REDIBIS_REGEX_PATTERNS")
    if env and Path(env).is_file():
        _cache = json.loads(Path(env).read_text(encoding="utf-8"))
        return _cache

    cwd_file = Path("regex_patterns.json")
    if cwd_file.is_file():
        _cache = json.loads(cwd_file.read_text(encoding="utf-8"))
        return _cache

    if _PACKAGED.is_file():
        _cache = json.loads(_PACKAGED.read_text(encoding="utf-8"))
        return _cache

    return {"patterns": {}}


def resolve_pattern(name: str,
                    source: Union[dict, str, None] = None) -> Optional[str]:
    """Resolve a named pattern to its regex string.

    Returns *None* if the name is not found in the library.
    """
    lib = load_regex_library(source)
    patterns = lib.get("patterns", {})
    entry = patterns.get(name)
    if entry is None:
        return None
    return entry.get("regex") if isinstance(entry, dict) else str(entry)


def list_patterns(source: Union[dict, str, None] = None) -> list[dict]:
    """List all registered patterns with metadata.

    Each item: ``{name, label, regex, description, category, examples}``.
    """
    lib = load_regex_library(source)
    out: list[dict] = []
    for name, entry in lib.get("patterns", {}).items():
        if isinstance(entry, dict):
            out.append({
                "name": name,
                "label": entry.get("label", name),
                "regex": entry.get("regex", ""),
                "description": entry.get("description", ""),
                "category": entry.get("category", ""),
                "examples": entry.get("examples", []),
            })
        else:
            out.append({
                "name": name, "label": name, "regex": str(entry),
                "description": "", "category": "", "examples": [],
            })
    return out


def clear_cache() -> None:
    """Clear the in-process library cache (useful in tests)."""
    global _cache
    _cache = None
