"""Batch job dashboard — aggregate run store for feature 4b."""

from __future__ import annotations

from typing import Any

from redibis.agents.lineage_store import LineageStore
from redibis.agents.run_models import RunStatus


def batch_dashboard(store: LineageStore, *, limit: int = 50) -> dict[str, Any]:
    """Summarize recent agent runs for the batch dashboard UI."""
    runs = store.list_runs(limit=limit)
    by_status: dict[str, int] = {}
    tables_seen: set[str] = set()
    enriched_runs: list[dict[str, Any]] = []
    for run in runs:
        st = run.get("status") or "unknown"
        by_status[st] = by_status.get(st, 0) + 1
        for t in run.get("tables") or []:
            tables_seen.add(t)
        entry = dict(run)
        try:
            full = store.load(run["run_id"])
            from redibis.agents.table_status import table_status_rows
            entry["table_status"] = table_status_rows(full)
        except (FileNotFoundError, KeyError, TypeError):
            entry["table_status"] = []
        enriched_runs.append(entry)

    return {
        "total_runs": len(runs),
        "by_status": by_status,
        "tables_covered": sorted(tables_seen),
        "runs": enriched_runs,
        "active": sum(1 for r in runs if r.get("status") == RunStatus.RUNNING.value),
    }
