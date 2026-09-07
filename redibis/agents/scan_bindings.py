"""Scan / config helpers for in-app pipeline execution."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from redibis.agents.run_state import TableRunState
from redibis.config import RedibisConfig
from redibis.scan.config import ScanConfig
from redibis.services.scan_service import to_scan_config


def build_scan_config(
    table: str,
    node_params: dict[str, Any],
    *,
    redibis_config: Optional[RedibisConfig] = None,
    scan_types: Optional[list[str]] = None,
) -> ScanConfig:
    """Build a ``ScanConfig`` for one table, merging node params over root config."""
    from redibis.agents.run_defaults import load_defaults, merge_defaults_into_params

    merged = merge_defaults_into_params(node_params)
    cfg = redibis_config or RedibisConfig.default()
    cfg = RedibisConfig.from_dict(cfg.to_dict())
    cfg.table = table

    if scan_types:
        cfg.scan_types = scan_types
    elif merged.get("equation_mode"):
        cfg.pii.equation_mode = str(merged["equation_mode"])

    if merged.get("pii_engines"):
        cfg.pii.engines = str(merged["pii_engines"])

    if merged.get("profiler_engine") or merged.get("engine"):
        cfg.profiling.engine = str(
            merged.get("profiler_engine") or merged.get("engine")
        )

    automerge = merged.get("automerge")
    if automerge:
        cfg.contract.automerge = str(automerge)

    # Planner defaults from pack when node omits provider (IntentPlanner uses agents config).
    try:
        defaults = load_defaults()
        planner = defaults.get("planner") or {}
        if not cfg.agents.planner_provider and planner.get("provider"):
            cfg.agents.planner_provider = str(planner["provider"])
        if not cfg.agents.planner_model and planner.get("model"):
            cfg.agents.planner_model = str(planner["model"])
    except Exception:
        pass

    return to_scan_config(cfg, table=table)


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def get_table_state(states: dict[str, TableRunState], table: str) -> TableRunState:
    if table not in states:
        states[table] = TableRunState(table=table)
    return states[table]
