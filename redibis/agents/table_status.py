"""Per-table batch status for the agent dashboard (Phase 5 T5.1)."""

from __future__ import annotations

from typing import Any

from redibis.agents.run_models import AgentRun, RunStatus, StepStatus


def _aggregate_table_status(steps: list[Any]) -> str:
    if not steps:
        return "queued"
    statuses = {s.status for s in steps}
    if StepStatus.FAILED in statuses:
        return "failed"
    if StepStatus.RUNNING in statuses:
        return "running"
    if all(s.status in (StepStatus.COMPLETED, StepStatus.SKIPPED) for s in steps):
        if any(
            s.status == StepStatus.SKIPPED
            and isinstance(s.output, dict)
            and "approval" in str(s.output.get("reason", "")).lower()
            for s in steps
        ):
            return "needs_review"
        if any(
            isinstance(s.output, dict) and s.output.get("escalations")
            for s in steps
        ):
            return "needs_review"
        return "done"
    return "partial"


def table_status_rows(run: AgentRun) -> list[dict[str, Any]]:
    """Per-table status for batch dashboard result nodes."""
    by_table: dict[str, list] = {}
    run_ids: dict[str, str] = {}

    for step in run.steps:
        if not step.table:
            continue
        by_table.setdefault(step.table, []).append(step)
        out = step.output or {}
        if out.get("run_id"):
            run_ids[step.table] = str(out["run_id"])

    rows: list[dict[str, Any]] = []
    paused_table = ""
    if run.status == RunStatus.AWAITING_HITL:
        paused_table = str((run.batch_meta or {}).get("paused_table") or "")
        if not paused_table:
            intr = (run.hitl_pending or {}).get("interrupts") or []
            if intr and isinstance(intr[0], dict):
                val = intr[0].get("value") or {}
                if isinstance(val, dict):
                    paused_table = str(val.get("table") or "")

    for table in run.tables or sorted(by_table.keys()):
        steps = by_table.get(table, [])
        status = _aggregate_table_status(steps)
        if paused_table and table == paused_table:
            status = "awaiting_hitl"
        elif run.status == RunStatus.CANCELLED and status in ("queued", "partial", "running"):
            status = "cancelled"
        rows.append({
            "table": table,
            "status": status,
            "run_id": run_ids.get(table, ""),
            "steps_completed": sum(1 for s in steps if s.status == StepStatus.COMPLETED),
            "steps_total": len(steps),
            "last_error": next((s.error for s in reversed(steps) if s.error), ""),
        })
    return rows


def run_summary_card(run: AgentRun, table: str) -> dict[str, Any]:
    """Summary stats for one table — reads step outputs, does not recompute scans."""
    steps = [s for s in run.steps if s.table == table]
    stats: dict[str, Any] = {
        "table": table,
        "status": _aggregate_table_status(steps),
        "agent_run_id": run.run_id,
        "run_name": run.name or run.run_id,
        "steps_done": [],
        "metrics": {},
    }

    flags = {
        "profiled": False,
        "pii_scanned": False,
        "classified": False,
        "masking_recommended": False,
        "contract_drafted": False,
        "quality_scanned": False,
    }
    columns = 0
    pii_columns = 0
    tags_applied = 0
    contract_version = ""

    for step in steps:
        out = step.output or {}
        if out.get("run_id"):
            stats["run_id"] = str(out["run_id"])
        if step.status != StepStatus.COMPLETED and step.status != StepStatus.SKIPPED:
            continue

        if step.node_kind in ("profile_scan", "profile"):
            flags["profiled"] = True
            columns = max(columns, int(out.get("columns") or 0))
        if step.node_kind == "pii_scan":
            flags["pii_scanned"] = True
            pii_columns = max(pii_columns, int(out.get("columns_detected") or 0))
        if step.node_kind == "classify":
            flags["classified"] = True
            tags = out.get("tags") or {}
            tags_applied = max(tags_applied, sum(len(v) for v in tags.values()))
        if step.node_kind == "mask":
            flags["masking_recommended"] = not out.get("skipped", True)
        if step.node_kind in ("contract_write", "contract"):
            if step.node_kind == "contract_write" or out.get("sub_steps"):
                flags["contract_drafted"] = True
            if out.get("contract_version"):
                contract_version = str(out["contract_version"])
        if step.node_kind == "quality_scan":
            flags["quality_scanned"] = True

        if out.get("sub_steps"):
            for sub in out["sub_steps"]:
                kind = sub.get("kind", "")
                if kind == "pii":
                    flags["pii_scanned"] = True
                    pii_columns = max(pii_columns, int(sub.get("columns_detected") or 0))
                if kind == "classify":
                    if not sub.get("skipped"):
                        flags["classified"] = True
                        tags = sub.get("tags") or out.get("tags") or {}
                        if isinstance(tags, dict):
                            tags_applied = max(tags_applied, sum(len(v) for v in tags.values()))
                if kind == "quality":
                    flags["quality_scanned"] = True
                if kind == "profile":
                    flags["profiled"] = True
                    columns = max(columns, int(sub.get("columns") or 0))

        if step.node_kind == "contract" and out.get("columns"):
            columns = max(columns, int(out.get("columns") or 0))

    stats["steps_done"] = [
        {"label": "Profiled", "done": flags["profiled"]},
        {"label": "PII scanned", "done": flags["pii_scanned"]},
        {"label": "Classified", "done": flags["classified"]},
        {"label": "Masking recommended", "done": flags["masking_recommended"]},
        {"label": "Contract drafted", "done": flags["contract_drafted"]},
    ]
    stats["metrics"] = {
        "columns": columns,
        "pii_columns": pii_columns,
        "tags_applied": tags_applied,
        "contract_version": contract_version or "—",
    }
    stats["last_error"] = next((s.error for s in reversed(steps) if s.error), "")
    return stats
