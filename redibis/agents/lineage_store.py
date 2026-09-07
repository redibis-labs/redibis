"""Run / lineage store — durable agent run records for DAG trace and audit."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Iterator, Optional

from redibis.agents.cancellation import CancellationToken
from redibis.agents.run_models import AgentRun, RunStatus


class LineageStore:
    """File-backed store for agent runs (one directory per run)."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._tokens: dict[str, CancellationToken] = {}
        self._lock = threading.Lock()
        from redibis.agents.run_log import configure_run_log_root

        configure_run_log_root(self.root)

    def _run_dir(self, run_id: str) -> Path:
        return self.root / run_id

    def _run_path(self, run_id: str) -> Path:
        return self._run_dir(run_id) / "run.json"

    def save_governance_state(self, run_id: str, state: dict) -> None:
        """Persist typed GovernanceState snapshot for replay."""
        path = self._run_dir(run_id) / "governance_state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def load_governance_state(self, run_id: str) -> Optional[dict]:
        path = self._run_dir(run_id) / "governance_state.json"
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def load_audit_events(self, run_id: str) -> list[dict]:
        from redibis.agents.run_log import load_audit_events

        return load_audit_events(self.root, run_id)

    def save(
        self,
        run: AgentRun,
        *,
        report_output_dir: Optional[Path] = None,
        scan_output_dir: Optional[Path] = None,
    ) -> None:
        from redibis.agents.run_log import sync_run_logs, persist_audit_events

        sync_run_logs(run, lineage_root=self.root)
        d = self._run_dir(run.run_id)
        d.mkdir(parents=True, exist_ok=True)
        self._run_path(run.run_id).write_text(
            json.dumps(run.to_dict(), indent=2),
            encoding="utf-8",
        )
        persist_audit_events(self.root, run.run_id)
        gov_path = d / "governance_state.json"
        from redibis.agents.run_log import snapshot_audit_events

        gov_payload = {
            "audit_event_count": len(snapshot_audit_events(run.run_id)),
            "run_id": run.run_id,
        }
        if not gov_path.is_file():
            gov_path.write_text(json.dumps(gov_payload, indent=2), encoding="utf-8")
        if report_output_dir is None:
            try:
                import os

                from redibis.config import RedibisConfig

                cfg = RedibisConfig.default()
                report_output_dir = Path(cfg.report.output_dir)
                scan_output_dir = Path(os.getenv("SCAN_OUTPUT_DIR", str(cfg.report.output_dir)))
            except Exception:
                report_output_dir = None
        if report_output_dir is not None:
            from redibis.agents.run_debug import write_run_debug_artifacts

            write_run_debug_artifacts(
                run,
                lineage_root=self.root,
                report_output_dir=Path(report_output_dir),
                scan_output_dir=scan_output_dir,
            )

    def load(self, run_id: str) -> AgentRun:
        path = self._run_path(run_id)
        if not path.is_file():
            raise FileNotFoundError(f"agent run not found: {run_id}")
        return AgentRun.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def list_runs(self, *, limit: int = 50) -> list[dict]:
        runs: list[dict] = []
        if not self.root.is_dir():
            return runs
        for child in sorted(self.root.iterdir(), reverse=True):
            if not child.is_dir():
                continue
            path = child / "run.json"
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                runs.append({
                    "run_id": data.get("run_id"),
                    "name": data.get("name"),
                    "status": data.get("status"),
                    "tables": data.get("tables") or [],
                    "created_at": data.get("created_at"),
                    "finished_at": data.get("finished_at"),
                    "ledger_summary": (data.get("ledger") or {}).get("summary"),
                })
            except (json.JSONDecodeError, OSError):
                continue
            if len(runs) >= limit:
                break
        return runs

    def delete(self, run_id: str) -> bool:
        """Remove a single run directory. Returns True if it existed."""
        import shutil
        d = self._run_dir(run_id)
        if not d.is_dir():
            return False
        shutil.rmtree(d, ignore_errors=True)
        self.drop_token(run_id)
        return True

    def clear(self, *, keep_running: bool = True) -> int:
        """Delete all run records. Skips runs still RUNNING/AWAITING_HITL when keep_running."""
        removed = 0
        for meta in self.list_runs(limit=10_000):
            rid = meta.get("run_id")
            if not rid:
                continue
            if keep_running and meta.get("status") in (
                RunStatus.RUNNING.value,
                RunStatus.AWAITING_HITL.value,
            ):
                continue
            if self.delete(rid):
                removed += 1
        return removed

    def cancellation_token(self, run_id: str) -> CancellationToken:
        with self._lock:
            if run_id not in self._tokens:
                self._tokens[run_id] = CancellationToken()
            return self._tokens[run_id]

    def request_cancel(self, run_id: str, reason: str = "") -> bool:
        try:
            run = self.load(run_id)
        except FileNotFoundError:
            return False
        if run.status in (RunStatus.COMPLETED, RunStatus.CANCELLED, RunStatus.FAILED):
            return False
        token = self.cancellation_token(run_id)
        token.cancel(reason or "cancelled via API")
        run.status = RunStatus.CANCELLED
        run.cancel_reason = token.reason
        self.save(run)
        return True

    def drop_token(self, run_id: str) -> None:
        with self._lock:
            self._tokens.pop(run_id, None)

    def iter_runs(self) -> Iterator[AgentRun]:
        for meta in self.list_runs(limit=10_000):
            yield self.load(meta["run_id"])
