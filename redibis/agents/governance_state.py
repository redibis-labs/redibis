"""Typed auditable state for agent governance (Phase 1 Ruling B)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


GOVERNANCE_STATE_VERSION = 1


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_hash(payload: Any) -> str:
    """Deterministic SHA-256 prefix for audit/replay."""
    try:
        text = json.dumps(payload, sort_keys=True, default=str)
    except (TypeError, ValueError):
        text = repr(payload)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


@dataclass
class GovernanceTransition:
    """One recorded field change (append-only)."""

    ts: str
    field: str
    old_hash: str
    new_hash: str
    actor: str = "agent"
    node: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "field": self.field,
            "old_hash": self.old_hash,
            "new_hash": self.new_hash,
            "actor": self.actor,
            "node": self.node,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GovernanceTransition":
        return cls(
            ts=str(data.get("ts") or ""),
            field=str(data.get("field") or ""),
            old_hash=str(data.get("old_hash") or ""),
            new_hash=str(data.get("new_hash") or ""),
            actor=str(data.get("actor") or "agent"),
            node=str(data.get("node") or ""),
        )


@dataclass
class GovernanceState:
    """Versioned, typed run state — every mutation is a recorded transition."""

    version: int = GOVERNANCE_STATE_VERSION
    run_id: str = ""
    agent_run_id: str = ""
    table: str = ""
    node_index: int = 0
    status: str = "pending"
    error: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)
    contract_version: str = ""
    det_version: str = ""
    prompt_hash: str = ""
    prior_version: str = ""
    transitions: list[GovernanceTransition] = field(default_factory=list)

    def get(self, field: str, default: Any = None) -> Any:
        return getattr(self, field, default)

    def transition(
        self,
        *,
        field: str,
        value: Any,
        actor: str = "agent",
        node: str = "",
    ) -> "GovernanceState":
        """Return a new state with ``field`` updated and the change logged."""
        if not hasattr(self, field):
            raise AttributeError(f"unknown governance field: {field}")
        old_val = getattr(self, field)
        old_hash = stable_hash(old_val)
        new_hash = stable_hash(value)
        if old_hash == new_hash:
            return self
        tx = GovernanceTransition(
            ts=_utc_iso(),
            field=field,
            old_hash=old_hash,
            new_hash=new_hash,
            actor=actor,
            node=node,
        )
        data = self.to_dict()
        data[field] = value
        data["transitions"] = [t.to_dict() for t in self.transitions] + [tx.to_dict()]
        return GovernanceState.from_dict(data)

    def record_contract_lineage(
        self,
        *,
        contract_version: str,
        det_version: str = "",
        prior_version: str = "",
        prompt_hash: str = "",
        actor: str = "agent",
        node: str = "enrich",
    ) -> "GovernanceState":
        state = self
        for fld, val in (
            ("contract_version", contract_version),
            ("det_version", det_version or self.det_version),
            ("prior_version", prior_version or self.prior_version),
            ("prompt_hash", prompt_hash or self.prompt_hash),
        ):
            if val:
                state = state.transition(field=fld, value=val, actor=actor, node=node)
        return state

    def to_graph_state(self) -> dict[str, Any]:
        """Bridge to LangGraph ``_GraphState``."""
        return {
            "table": self.table,
            "node_index": self.node_index,
            "steps": list(self.steps),
            "status": self.status,
            "error": self.error,
        }

    @classmethod
    def from_graph_state(
        cls,
        graph: dict[str, Any],
        *,
        run_id: str = "",
        agent_run_id: str = "",
        base: Optional["GovernanceState"] = None,
    ) -> "GovernanceState":
        """Sync from LangGraph checkpoint without losing lineage fields."""
        base = base or cls(run_id=run_id, agent_run_id=agent_run_id)
        state = base
        mapping = {
            "table": graph.get("table", ""),
            "node_index": int(graph.get("node_index") or 0),
            "steps": list(graph.get("steps") or []),
            "status": str(graph.get("status") or "pending"),
            "error": str(graph.get("error") or ""),
        }
        for fld, val in mapping.items():
            if state.get(fld) != val:
                state = state.transition(field=fld, value=val, actor="agent")
        return state

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "run_id": self.run_id,
            "agent_run_id": self.agent_run_id,
            "table": self.table,
            "node_index": self.node_index,
            "status": self.status,
            "error": self.error,
            "steps": list(self.steps),
            "contract_version": self.contract_version,
            "det_version": self.det_version,
            "prompt_hash": self.prompt_hash,
            "prior_version": self.prior_version,
            "transitions": [t.to_dict() for t in self.transitions],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GovernanceState":
        return cls(
            version=int(data.get("version") or GOVERNANCE_STATE_VERSION),
            run_id=str(data.get("run_id") or ""),
            agent_run_id=str(data.get("agent_run_id") or ""),
            table=str(data.get("table") or ""),
            node_index=int(data.get("node_index") or 0),
            status=str(data.get("status") or "pending"),
            error=str(data.get("error") or ""),
            steps=list(data.get("steps") or []),
            contract_version=str(data.get("contract_version") or ""),
            det_version=str(data.get("det_version") or ""),
            prompt_hash=str(data.get("prompt_hash") or ""),
            prior_version=str(data.get("prior_version") or ""),
            transitions=[
                GovernanceTransition.from_dict(t)
                for t in (data.get("transitions") or [])
            ],
        )
