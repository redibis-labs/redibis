"""Agent run / batch job models and task ledger."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional
from uuid import uuid4


def _new_run_id() -> str:
    return uuid4().hex[:16]


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_HITL = "awaiting_hitl"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskStatus(str, Enum):
    PLANNED = "planned"
    RUNNING = "running"
    DONE = "done"
    SKIPPED = "skipped"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class StepRecord:
    """One executed (or skipped) pipeline step for lineage / DAG trace."""

    step_id: str
    node_id: str
    node_kind: str
    tool: str
    table: str = ""
    status: StepStatus = StepStatus.PENDING
    started_at: str = ""
    finished_at: str = ""
    error: str = ""
    output: dict[str, Any] = field(default_factory=dict)
    span_id: str = ""
    attempts: int = 1
    max_retries: int = 2
    validation: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        out = {
            "step_id": self.step_id,
            "node_id": self.node_id,
            "node_kind": self.node_kind,
            "tool": self.tool,
            "table": self.table,
            "status": self.status.value,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "output": dict(self.output),
            "span_id": self.span_id,
            "attempts": self.attempts,
            "max_retries": self.max_retries,
        }
        if self.validation is not None:
            out["validation"] = dict(self.validation)
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StepRecord":
        return cls(
            step_id=str(data.get("step_id") or ""),
            node_id=str(data.get("node_id") or ""),
            node_kind=str(data.get("node_kind") or ""),
            tool=str(data.get("tool") or ""),
            table=str(data.get("table") or ""),
            status=StepStatus(data.get("status") or StepStatus.PENDING.value),
            started_at=str(data.get("started_at") or ""),
            finished_at=str(data.get("finished_at") or ""),
            error=str(data.get("error") or ""),
            output=dict(data.get("output") or {}),
            span_id=str(data.get("span_id") or ""),
            attempts=int(data.get("attempts") or 1),
            max_retries=int(data.get("max_retries") or 2),
            validation=dict(data.get("validation")) if data.get("validation") else None,
        )


@dataclass
class TaskEntry:
    """Planned unit of work: one pipeline step for one table."""

    task_id: str
    table: str
    node_id: str
    node_kind: str
    status: TaskStatus = TaskStatus.PLANNED
    step_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "table": self.table,
            "node_id": self.node_id,
            "node_kind": self.node_kind,
            "status": self.status.value,
            "step_id": self.step_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskEntry":
        return cls(
            task_id=str(data.get("task_id") or ""),
            table=str(data.get("table") or ""),
            node_id=str(data.get("node_id") or ""),
            node_kind=str(data.get("node_kind") or ""),
            status=TaskStatus(data.get("status") or TaskStatus.PLANNED.value),
            step_id=str(data.get("step_id") or ""),
        )


@dataclass
class TaskLedger:
    """Planned vs accomplished reconciliation."""

    tasks: list[TaskEntry] = field(default_factory=list)

    def planned_count(self) -> int:
        return sum(1 for t in self.tasks if t.status == TaskStatus.PLANNED)

    def done_count(self) -> int:
        return sum(1 for t in self.tasks if t.status == TaskStatus.DONE)

    def reconcile(self, steps: Optional[list] = None) -> dict[str, Any]:
        by_status: dict[str, int] = {}
        for task in self.tasks:
            by_status[task.status.value] = by_status.get(task.status.value, 0) + 1
        summary: dict[str, Any] = {
            "total": len(self.tasks),
            "planned": self.planned_count(),
            "done": self.done_count(),
            "by_status": by_status,
            "complete": self.planned_count() == 0 and self.done_count() == len(self.tasks),
        }
        if steps:
            retried = sum(1 for s in steps if getattr(s, "attempts", 1) > 1)
            degraded = sum(
                1 for s in steps
                if isinstance(getattr(s, "output", None), dict) and s.output.get("degraded")
            )
            summary["retried_steps"] = retried
            summary["degraded_steps"] = degraded
        return summary

    def to_dict(self) -> dict[str, Any]:
        return {
            "tasks": [t.to_dict() for t in self.tasks],
            "summary": self.reconcile(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskLedger":
        return cls(tasks=[TaskEntry.from_dict(t) for t in (data.get("tasks") or [])])


@dataclass
class AgentRun:
    """A batch agent run with pipeline spec, steps, and telemetry."""

    run_id: str = field(default_factory=_new_run_id)
    name: str = ""
    status: RunStatus = RunStatus.PENDING
    pipeline: dict[str, Any] = field(default_factory=dict)
    tables: list[str] = field(default_factory=list)
    steps: list[StepRecord] = field(default_factory=list)
    ledger: TaskLedger = field(default_factory=TaskLedger)
    telemetry: list[dict[str, Any]] = field(default_factory=list)
    cancel_reason: str = ""
    error: str = ""
    created_at: str = ""
    finished_at: str = ""
    hitl_pending: dict[str, Any] = field(default_factory=dict)
    session_id: str = ""
    batch_meta: dict[str, Any] = field(default_factory=dict)
    logs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "name": self.name,
            "status": self.status.value,
            "pipeline": dict(self.pipeline),
            "tables": list(self.tables),
            "steps": [s.to_dict() for s in self.steps],
            "ledger": self.ledger.to_dict(),
            "telemetry": list(self.telemetry),
            "cancel_reason": self.cancel_reason,
            "error": self.error,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "hitl_pending": dict(self.hitl_pending),
            "session_id": self.session_id,
            "batch_meta": dict(self.batch_meta),
            "logs": list(self.logs),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentRun":
        return cls(
            run_id=str(data.get("run_id") or _new_run_id()),
            name=str(data.get("name") or ""),
            status=RunStatus(data.get("status") or RunStatus.PENDING.value),
            pipeline=dict(data.get("pipeline") or {}),
            tables=list(data.get("tables") or []),
            steps=[StepRecord.from_dict(s) for s in (data.get("steps") or [])],
            ledger=TaskLedger.from_dict(data.get("ledger") or {}),
            telemetry=list(data.get("telemetry") or []),
            cancel_reason=str(data.get("cancel_reason") or ""),
            error=str(data.get("error") or ""),
            created_at=str(data.get("created_at") or ""),
            finished_at=str(data.get("finished_at") or ""),
            hitl_pending=dict(data.get("hitl_pending") or {}),
            session_id=str(data.get("session_id") or ""),
            batch_meta=dict(data.get("batch_meta") or {}),
            logs=list(data.get("logs") or []),
        )
