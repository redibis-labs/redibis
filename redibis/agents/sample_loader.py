"""Resolve sample CSV/Parquet paths for pipeline execution."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import pandas as pd


def resolve_sample_path(
    table: str,
    *,
    sample_paths: Optional[dict[str, str]] = None,
    sample_dir: Optional[Path] = None,
    node_params: Optional[dict[str, Any]] = None,
) -> Optional[Path]:
    """Find a data file for ``table`` from explicit paths or a sample directory."""
    node_params = node_params or {}
    if node_params.get("sample_path"):
        p = Path(str(node_params["sample_path"]))
        if p.is_file():
            return p

    for key in (table, table.replace(".", "_")):
        if sample_paths and key in sample_paths:
            p = Path(sample_paths[key])
            if p.is_file():
                return p

    if sample_dir is None:
        return None

    root = Path(sample_dir)
    db, _, tbl = table.partition(".")
    candidates = [
        root / f"{table}.csv",
        root / f"{table}.parquet",
        root / f"{db}_{tbl}.csv",
        root / f"{tbl}.csv",
        root / f"telco_{tbl}.csv",
        root / f"realistic_{tbl}.csv",
    ]
    for path in candidates:
        if path.is_file():
            return path
    return None


def load_sample_dataframe(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix == ".parquet":
        return pd.read_parquet(path)
    if suffix in (".xlsx", ".xls"):
        return pd.read_excel(path)
    return pd.read_csv(path)
