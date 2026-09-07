"""Agentic session manifest — durable UI resume + per-table run references (Phase 3)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from redibis.agents.models import PipelineSpec, PromptPlan
from redibis.agents.run_models import RunStatus


def _new_session_id() -> str:
    return uuid4().hex[:16]


@dataclass
class TableRunRef:
    """Reference to a normal redibis run folder (not nested under agentic dir)."""

    table: str
    run_id: str
    redibis_session_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "run_id": self.run_id,
            "redibis_session_id": self.redibis_session_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TableRunRef":
        return cls(
            table=str(data.get("table") or ""),
            run_id=str(data.get("run_id") or ""),
            redibis_session_id=str(data.get("redibis_session_id") or ""),
        )


@dataclass
class AgenticSession:
    """
    Agentic session index — spec, prompt plan, queue, and per-table run refs.

    LangGraph checkpointer handles execution resume; this manifest handles UI resume.
    """

    session_id: str = field(default_factory=_new_session_id)
    name: str = ""
    status: RunStatus = RunStatus.PENDING
    spec: dict[str, Any] = field(default_factory=dict)
    prompt_plan: dict[str, Any] = field(default_factory=dict)
    table_runs: list[TableRunRef] = field(default_factory=list)
    langgraph_thread_id: str = ""
    cancel_reason: str = ""
    error: str = ""
    created_at: str = ""
    finished_at: str = ""

    def record_table_run(self, table: str, run_id: str, *, session_id: str = "") -> None:
        for ref in self.table_runs:
            if ref.table == table:
                ref.run_id = run_id
                if session_id:
                    ref.redibis_session_id = session_id
                return
        self.table_runs.append(TableRunRef(
            table=table,
            run_id=run_id,
            redibis_session_id=session_id,
        ))

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "name": self.name,
            "status": self.status.value,
            "spec": dict(self.spec),
            "prompt_plan": dict(self.prompt_plan),
            "table_runs": [r.to_dict() for r in self.table_runs],
            "langgraph_thread_id": self.langgraph_thread_id,
            "cancel_reason": self.cancel_reason,
            "error": self.error,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgenticSession":
        return cls(
            session_id=str(data.get("session_id") or _new_session_id()),
            name=str(data.get("name") or ""),
            status=RunStatus(data.get("status") or RunStatus.PENDING.value),
            spec=dict(data.get("spec") or {}),
            prompt_plan=dict(data.get("prompt_plan") or {}),
            table_runs=[TableRunRef.from_dict(r) for r in (data.get("table_runs") or [])],
            langgraph_thread_id=str(data.get("langgraph_thread_id") or ""),
            cancel_reason=str(data.get("cancel_reason") or ""),
            error=str(data.get("error") or ""),
            created_at=str(data.get("created_at") or ""),
            finished_at=str(data.get("finished_at") or ""),
        )

    @classmethod
    def from_spec(
        cls,
        spec: PipelineSpec,
        plan: Optional[PromptPlan] = None,
        *,
        session_id: str = "",
    ) -> "AgenticSession":
        return cls(
            session_id=session_id or _new_session_id(),
            name=spec.name,
            spec=spec.to_dict(),
            prompt_plan=plan.to_dict() if plan is not None else {},
        )


def save_manifest(root: Path, session: AgenticSession) -> Path:
    d = root / session.session_id
    d.mkdir(parents=True, exist_ok=True)
    path = d / "manifest.json"
    path.write_text(json.dumps(session.to_dict(), indent=2), encoding="utf-8")
    return path


def load_manifest(root: Path, session_id: str) -> AgenticSession:
    path = root / session_id / "manifest.json"
    if not path.is_file():
        raise FileNotFoundError(f"agentic session not found: {session_id}")
    return AgenticSession.from_dict(json.loads(path.read_text(encoding="utf-8")))
