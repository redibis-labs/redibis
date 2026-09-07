"""Portable run-prefix discovery for flat and session-nested storage keys."""

from __future__ import annotations

from typing import Iterable, Optional

WORKFLOW_PREFIXES = ("scan", "pii", "ge", "enrich")


def storage_prefix_for_key(key: str, run_id: str | None = None) -> Optional[str]:
    """Return ``workflow/table_safe/{session_id/}{run_id}/`` for a storage key.

    Supports both ``{wf}/{table}/{run_id}/…`` and
    ``{wf}/{table}/{session_id}/{run_id}/…``.
    """
    parts = str(key or "").replace("\\", "/").split("/")
    if len(parts) < 4:
        return None
    wf, table_safe = parts[0], parts[1]
    rid = (run_id or "").strip()
    if rid:
        if parts[2] == rid:
            return f"{wf}/{table_safe}/{rid}/"
        if len(parts) >= 5 and parts[3] == rid:
            return f"{wf}/{table_safe}/{parts[2]}/{rid}/"
        return None
    if len(parts) >= 5:
        return f"{wf}/{table_safe}/{parts[2]}/{parts[3]}/"
    return f"{wf}/{table_safe}/{parts[2]}/"


def prefixes_for_run_id(keys: Iterable[str], run_id: str) -> list[str]:
    """Unique storage prefixes whose path contains *run_id* as the run segment."""
    seen: set[str] = set()
    out: list[str] = []
    rid = (run_id or "").strip()
    if not rid:
        return out
    for key in keys:
        prefix = storage_prefix_for_key(key, rid)
        if prefix and prefix not in seen:
            seen.add(prefix)
            out.append(prefix)
    return out


def table_from_prefix(prefix: str) -> str:
    parts = str(prefix or "").replace("\\", "/").split("/")
    if len(parts) < 2:
        return ""
    table_safe = parts[1]
    return table_safe.replace("_", ".", 1) if "_" in table_safe else table_safe
