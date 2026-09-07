"""Run-level debug/trace artifacts for agent board downloads."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from redibis.agents.artifacts import _run_roots
from redibis.agents.run_models import AgentRun, RunStatus


def _task_log_paths(
    run: AgentRun,
    *,
    report_output_dir: Path,
    scan_output_dir: Optional[Path],
) -> list[tuple[str, str, Path]]:
    """Return (table, scan_run_id, log_path) for each step with a persisted task log."""
    found: list[tuple[str, str, Path]] = []
    seen: set[tuple[str, str]] = set()
    for step in run.steps:
        out = step.output or {}
        scan_run_id = str(out.get("run_id") or "")
        table = step.table or ""
        if not scan_run_id or not table:
            continue
        key = (table, scan_run_id)
        if key in seen:
            continue
        seen.add(key)
        for root in _run_roots(
            scan_run_id,
            report_output_dir=report_output_dir,
            scan_output_dir=scan_output_dir,
        ):
            candidate = root / f"{table}.{scan_run_id}.log"
            if candidate.is_file():
                found.append((table, scan_run_id, candidate))
                break
    return found


def write_run_debug_artifacts(
    run: AgentRun,
    *,
    lineage_root: Path,
    report_output_dir: Path,
    scan_output_dir: Optional[Path] = None,
) -> None:
    """Persist ``run_trace.json`` and ``run_debug.log`` under the agent lineage run dir."""
    agent_dir = Path(lineage_root) / run.run_id
    agent_dir.mkdir(parents=True, exist_ok=True)

    status = run.status.value if hasattr(run.status, "value") else str(run.status)
    trace = {
        "run_id": run.run_id,
        "name": run.name,
        "status": status,
        "error": run.error,
        "cancel_reason": run.cancel_reason,
        "tables": list(run.tables),
        "created_at": run.created_at,
        "finished_at": run.finished_at,
        "steps": [step.to_dict() for step in run.steps],
        "telemetry": list(run.telemetry),
        "ledger": run.ledger.to_dict() if run.ledger else {},
    }
    (agent_dir / "run_trace.json").write_text(
        json.dumps(trace, indent=2),
        encoding="utf-8",
    )

    sections: list[str] = []
    log_path = agent_dir / "run.log"
    if log_path.is_file():
        try:
            hub_lines = log_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            hub_lines = []
    else:
        hub_lines = list(run.logs or [])
    if hub_lines:
        sections.append("=== live hub log ===")
        sections.extend(hub_lines)

    for table, scan_run_id, path in _task_log_paths(
        run,
        report_output_dir=report_output_dir,
        scan_output_dir=scan_output_dir,
    ):
        try:
            body = path.read_text(encoding="utf-8").rstrip()
        except OSError:
            continue
        if not body:
            continue
        sections.append(f"=== {table} · {scan_run_id} ===")
        sections.append(body)

    if run.error:
        sections.append("=== run error ===")
        sections.append(run.error)

    if run.status in (RunStatus.FAILED, RunStatus.CANCELLED) and not sections:
        sections.append(f"run ended with status={status}")

    (agent_dir / "run_debug.log").write_text(
        "\n".join(sections).rstrip() + ("\n" if sections else ""),
        encoding="utf-8",
    )
