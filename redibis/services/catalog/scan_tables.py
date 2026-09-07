"""Resolve ``schema.table`` identities from a scan-output folder."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional, Union

from redibis.services.catalog.mapping import split_physical_name


def tables_from_scan_dir(scan_dir: Union[str, Path]) -> list[str]:
    """
    Discover unique ``schema.table`` identities from session folders.

    Reads each ``*/session.json`` under ``scan_dir`` and extracts ``table_name``.
    Invalid or missing identities are skipped.
    """
    root = Path(scan_dir)
    if not root.is_dir():
        raise NotADirectoryError(f"Scan directory not found: {root}")

    tables: list[str] = []
    seen: set[str] = set()
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        state_file = child / "session.json"
        if not state_file.is_file():
            continue
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        table = (state.get("table_name") or "").strip()
        if not table:
            cfg_file = child / "session_config.yaml"
            if cfg_file.is_file():
                try:
                    import yaml
                    cfg = yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}
                    table = str(cfg.get("table_name") or "").strip()
                except Exception:
                    table = ""
        if not table or table in seen:
            continue
        try:
            split_physical_name(table)
        except ValueError:
            continue
        seen.add(table)
        tables.append(table)
    return tables


def filter_tables(
    candidates: list[str],
    *,
    database: str = "",
    include: Optional[list[str]] = None,
    exclude: Optional[list[str]] = None,
    tables_file: Optional[Union[str, Path]] = None,
) -> list[str]:
    """
    Apply database / include / exclude / tables-file filters.

    Order: candidates → database prefix → include (union of patterns/file) → exclude.
    """
    import fnmatch

    selected = list(candidates)
    db = (database or "").strip()
    if db:
        prefix = f"{db}."
        selected = [t for t in selected if t.startswith(prefix)]

    include_patterns: list[str] = list(include or [])
    if tables_file:
        path = Path(tables_file)
        if not path.is_file():
            raise FileNotFoundError(f"tables file not found: {path}")
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            include_patterns.append(line)

    if include_patterns:
        kept: list[str] = []
        for t in selected:
            if any(_match_table(t, pat) for pat in include_patterns):
                kept.append(t)
        selected = kept

    if exclude:
        selected = [
            t for t in selected
            if not any(_match_table(t, pat) for pat in exclude)
        ]
    return selected


def _match_table(table: str, pattern: str) -> bool:
    import fnmatch

    pat = (pattern or "").strip()
    if not pat:
        return False
    if pat == table:
        return True
    return fnmatch.fnmatch(table, pat)
