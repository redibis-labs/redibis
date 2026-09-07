"""Handoff from agentic run → manual 360° scan session (Phase 5 T5.3)."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from redibis.agents.run_models import AgentRun


def _run_id_for_table(run: AgentRun, table: str) -> str:
    for step in reversed(run.steps):
        if step.table != table:
            continue
        rid = (step.output or {}).get("run_id")
        if rid:
            return str(rid)
    return ""


def _artifact_paths(run_dir: Path) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    mapping = {
        "interactive_review.html": "interactive_review",
        "triage_report.html": "triage_report",
        "pii_regex_review.html": "pii_regex_review",
        "quality_contract.yaml": "quality_contract",
        "pii_contract.yaml": "pii_contract",
        "pii_detections.html": "pii_detection_report",
        "evidence_bundle.json": "evidence_bundle",
    }
    for name, key in mapping.items():
        p = run_dir / name
        if p.is_file():
            artifacts[key] = str(p)
    for report in sorted(run_dir.glob("quality-report-*.html")):
        artifacts["quality_report"] = str(report)
        break
    ge = run_dir / "ge_report" / "index.html"
    if ge.is_file():
        artifacts["ge_report"] = str(ge)
    return artifacts


def _load_quality_results(run_dir: Path) -> list[dict]:
    qpath = run_dir / "quality_results.json"
    if not qpath.is_file():
        return []
    try:
        data = json.loads(qpath.read_text(encoding="utf-8"))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def materialize_session_folder(
    *,
    table: str,
    run_id: str,
    source_run_dir: Path,
    session_dir: Path,
    sample_path: Optional[Path] = None,
) -> Path:
    """
    Write a minimal ``session.json`` so ``rehydrate_scan_session`` can load the run.

    Uses ``run_id`` as ``session_id`` per handover (references normal run folder).
    """
    session_dir.mkdir(parents=True, exist_ok=True)

    data_dest = session_dir / "data.csv"
    if sample_path and sample_path.is_file() and not data_dest.exists():
        shutil.copy2(sample_path, data_dest)
    elif not data_dest.exists() and (source_run_dir / "sample.csv").is_file():
        shutil.copy2(source_run_dir / "sample.csv", data_dest)

    artifacts = _artifact_paths(source_run_dir)
    quality_results = _load_quality_results(source_run_dir)
    now = datetime.now(timezone.utc).isoformat()
    quality_run = {
        "run_id": run_id,
        "run_type": "scan_quality",
        "started_at": now,
        "completed_at": now,
        "status": "complete",
        "run_kind": "scan",
        "quality_results": quality_results,
        "quality_total": len(quality_results),
        "quality_passed": sum(1 for r in quality_results if r.get("success")),
    }
    quality_run["quality_failed"] = (
        quality_run["quality_total"] - quality_run["quality_passed"]
    )
    state = {
        "session_id": run_id,
        "table_name": table,
        "status": "scan_complete",
        "artifacts": artifacts,
        "quality_passed": quality_run["quality_passed"],
        "quality_total": quality_run["quality_total"],
        "runs": [quality_run],
    }
    (session_dir / "session.json").write_text(
        json.dumps(state, indent=2),
        encoding="utf-8",
    )

    for name in (
        "pii_contract.yaml", "quality_contract.yaml", "interactive_review.html",
        "pii_detections.html", "pii_regex_review.html", "triage_report.html",
    ):
        src = source_run_dir / name
        if src.is_file() and not (session_dir / name).exists():
            shutil.copy2(src, session_dir / name)

    qresults_src = source_run_dir / "quality_results.json"
    if qresults_src.is_file():
        runs_dir = session_dir / "runs" / run_id
        runs_dir.mkdir(parents=True, exist_ok=True)
        qdest = runs_dir / "quality_results.json"
        if not qdest.exists():
            shutil.copy2(qresults_src, qdest)

    return session_dir


def handoff_to_manual_session(
    run: AgentRun,
    table: str,
    *,
    scan_output_dir: Path,
    runs_output_dir: Path,
    sample_path: Optional[Path] = None,
    session_manager: Any = None,
) -> dict[str, Any]:
    """
    Set redibis ``session_id`` to the table run, rehydrate, return redirect URL.

    Does not modify manual 360° pages — opens ``/?session=<run_id>``.
    """
    from redibis.services.session.state import rehydrate_scan_session

    run_id = _run_id_for_table(run, table)
    if not run_id:
        raise ValueError(f"no run_id recorded for table {table!r} in agent run {run.run_id}")

    source_run_dir = runs_output_dir / run_id
    session_dir = scan_output_dir / run_id

    if not session_dir.is_dir() or not (session_dir / "session.json").exists():
        if not source_run_dir.is_dir():
            source_run_dir = scan_output_dir / run_id
        materialize_session_folder(
            table=table,
            run_id=run_id,
            source_run_dir=source_run_dir,
            session_dir=session_dir,
            sample_path=sample_path,
        )

    # Manual 360° needs a data file; without it the session opens empty and the
    # masking/preview endpoints crash. Fail clearly instead.
    has_data = (session_dir / "data.csv").is_file() or (session_dir / "session_data").is_file()
    if not has_data:
        raise ValueError(
            f"no sample data persisted for {table!r} (run {run_id}); "
            "run a profile/PII/quality step for this table before handing off to the console"
        )

    session = rehydrate_scan_session(session_dir, scan_output_dir)
    if session is None:
        raise ValueError(f"failed to rehydrate session for {table!r} at {session_dir}")

    if session_manager is not None:
        session_manager._sessions[session.session_id] = session

    return {
        "session_id": session.session_id,
        "table": table,
        "run_id": run_id,
        "redirect_url": f"/?session={session.session_id}",
        "session_dir": str(session_dir),
    }
