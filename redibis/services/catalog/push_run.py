"""Durable catalog push-run manifests for resumable scan-folder batches."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union

_RUN_DIR = "catalog_push_runs"
_STATUSES = frozenset({
    "pending", "running", "succeeded", "failed", "excluded", "skipped",
})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class CatalogPushTask:
    table: str
    status: str = "pending"
    contract_version: str = ""
    entity_fqn: str = ""
    entity_id: str = ""
    contract_id: str = ""
    error: str = ""
    attempts: int = 0
    started_at: str = ""
    finished_at: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CatalogPushTask":
        status = str(data.get("status") or "pending")
        if status not in _STATUSES:
            status = "pending"
        return cls(
            table=str(data.get("table") or ""),
            status=status,
            contract_version=str(data.get("contract_version") or ""),
            entity_fqn=str(data.get("entity_fqn") or ""),
            entity_id=str(data.get("entity_id") or ""),
            contract_id=str(data.get("contract_id") or ""),
            error=str(data.get("error") or ""),
            attempts=int(data.get("attempts") or 0),
            started_at=str(data.get("started_at") or ""),
            finished_at=str(data.get("finished_at") or ""),
            details=dict(data.get("details") or {}),
        )


@dataclass
class CatalogPushRun:
    run_id: str
    scan_dir: str
    backend: str = "openmetadata"
    dry_run: bool = False
    started_at: str = field(default_factory=_utc_now)
    finished_at: str = ""
    filters: dict[str, Any] = field(default_factory=dict)
    tasks: list[CatalogPushTask] = field(default_factory=list)
    om_version: str = ""
    contract_strategy: str = ""  # odcs | native | ""

    def task_map(self) -> dict[str, CatalogPushTask]:
        return {t.table: t for t in self.tasks if t.table}

    def succeeded(self) -> list[CatalogPushTask]:
        return [t for t in self.tasks if t.status == "succeeded"]

    def failed(self) -> list[CatalogPushTask]:
        return [t for t in self.tasks if t.status == "failed"]

    def pending_or_retry(self) -> list[CatalogPushTask]:
        return [t for t in self.tasks if t.status in ("pending", "failed", "running")]

    def summary(self) -> dict[str, int]:
        counts = {
            "total": len(self.tasks),
            "succeeded": 0,
            "failed": 0,
            "pending": 0,
            "excluded": 0,
            "skipped": 0,
            "running": 0,
        }
        for t in self.tasks:
            if t.status in counts:
                counts[t.status] += 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "scan_dir": self.scan_dir,
            "backend": self.backend,
            "dry_run": self.dry_run,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "filters": self.filters,
            "om_version": self.om_version,
            "contract_strategy": self.contract_strategy,
            "tasks": [t.to_dict() for t in self.tasks],
            "summary": self.summary(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CatalogPushRun":
        tasks = [CatalogPushTask.from_dict(t) for t in (data.get("tasks") or [])]
        return cls(
            run_id=str(data.get("run_id") or ""),
            scan_dir=str(data.get("scan_dir") or ""),
            backend=str(data.get("backend") or "openmetadata"),
            dry_run=bool(data.get("dry_run")),
            started_at=str(data.get("started_at") or ""),
            finished_at=str(data.get("finished_at") or ""),
            filters=dict(data.get("filters") or {}),
            tasks=tasks,
            om_version=str(data.get("om_version") or ""),
            contract_strategy=str(data.get("contract_strategy") or ""),
        )


class CatalogPushRunStore:
    """Filesystem store for ``catalog_push_runs/<run_id>/manifest.json``."""

    def __init__(self, scan_dir: Union[str, Path]):
        self.scan_dir = Path(scan_dir).resolve()
        self.root = self.scan_dir / _RUN_DIR

    def create(
        self,
        tables: list[str],
        *,
        backend: str,
        dry_run: bool = False,
        filters: Optional[dict[str, Any]] = None,
        versions: Optional[dict[str, str]] = None,
        excluded: Optional[dict[str, str]] = None,
    ) -> CatalogPushRun:
        run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
        versions = versions or {}
        excluded = excluded or {}
        tasks: list[CatalogPushTask] = []
        for table in tables:
            if table in excluded:
                tasks.append(CatalogPushTask(
                    table=table,
                    status="excluded",
                    contract_version=versions.get(table, ""),
                    error=excluded[table],
                    finished_at=_utc_now(),
                ))
            else:
                tasks.append(CatalogPushTask(
                    table=table,
                    status="pending",
                    contract_version=versions.get(table, ""),
                ))
        run = CatalogPushRun(
            run_id=run_id,
            scan_dir=str(self.scan_dir),
            backend=backend,
            dry_run=dry_run,
            filters=dict(filters or {}),
            tasks=tasks,
        )
        self.save(run)
        return run

    def run_dir(self, run_id: str) -> Path:
        return self.root / run_id

    def manifest_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "manifest.json"

    def save(self, run: CatalogPushRun) -> Path:
        d = self.run_dir(run.run_id)
        d.mkdir(parents=True, exist_ok=True)
        path = d / "manifest.json"
        path.write_text(json.dumps(run.to_dict(), indent=2, default=str), encoding="utf-8")
        (d / "succeeded.json").write_text(
            json.dumps([t.to_dict() for t in run.succeeded()], indent=2),
            encoding="utf-8",
        )
        (d / "failed.json").write_text(
            json.dumps([t.to_dict() for t in run.failed()], indent=2),
            encoding="utf-8",
        )
        return path

    def load(self, run_id_or_path: str) -> CatalogPushRun:
        candidate = Path(run_id_or_path)
        if candidate.is_file():
            path = candidate
        elif candidate.is_dir() and (candidate / "manifest.json").is_file():
            path = candidate / "manifest.json"
        else:
            path = self.manifest_path(run_id_or_path)
        if not path.is_file():
            raise FileNotFoundError(f"catalog push run not found: {run_id_or_path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        return CatalogPushRun.from_dict(data)

    def resolve_resume(
        self,
        run: CatalogPushRun,
        *,
        active_versions: dict[str, str],
        retry_failed: bool = True,
    ) -> list[str]:
        """Return table names that still need a push attempt."""
        todo: list[str] = []
        for task in run.tasks:
            if task.status == "excluded":
                continue
            active_ver = active_versions.get(task.table, "")
            if task.status == "succeeded":
                # Re-queue when active version changed (empty stored version counts as stale).
                if active_ver and active_ver != (task.contract_version or ""):
                    task.status = "pending"
                    task.error = "active contract version changed since last success"
                    todo.append(task.table)
                continue
            if task.status in ("pending", "running"):
                todo.append(task.table)
                continue
            if task.status == "failed" and retry_failed:
                todo.append(task.table)
        return todo
