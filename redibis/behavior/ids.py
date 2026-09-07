"""
redibis.behavior.ids
====================
Safe identifiers for policy / outcome filesystem paths.

Rejects path traversal (``..``, separators) and unbound characters so store
and learning layers cannot write outside their base directory.
"""

from __future__ import annotations

import re

_SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
_SAFE_VERSION_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._+-]{0,63}$")


def validate_policy_id(value: str) -> str:
    """Return a sanitized policy id or raise ValueError."""
    return _validate(value, kind="id", pattern=_SAFE_ID_RE)


def validate_policy_version(value: str) -> str:
    """Return a sanitized policy version or raise ValueError."""
    return _validate(value, kind="version", pattern=_SAFE_VERSION_RE)


def validate_rule_id(value: str) -> str:
    """Return a sanitized rule id for outcome filenames or raise ValueError."""
    return _validate(value, kind="rule_id", pattern=_SAFE_ID_RE)


def _validate(value: str, *, kind: str, pattern: re.Pattern[str]) -> str:
    v = (value or "").strip()
    if not v:
        raise ValueError(f"invalid policy {kind}: empty")
    if ".." in v or "/" in v or "\\" in v or "\x00" in v:
        raise ValueError(f"invalid policy {kind}: path characters not allowed ({value!r})")
    if not pattern.match(v):
        raise ValueError(
            f"invalid policy {kind} {value!r} "
            f"(use a-z, A-Z, 0-9, ._-; optional + for versions)"
        )
    return v
