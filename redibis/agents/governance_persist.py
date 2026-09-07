"""Shared governance persistence for LangGraph and sequential executors."""

from __future__ import annotations

from typing import Any, Optional

from redibis.agents.governance_state import GovernanceState
from redibis.agents.lineage_store import LineageStore
from redibis.agents.run_models import StepRecord


def save_step_governance(
    lineage: LineageStore,
    *,
    step: StepRecord,
    table: str,
    node_index: int,
    run_id: str,
    agent_run_id: str,
    prior_gov: Optional[GovernanceState] = None,
    prior_steps: Optional[list[dict[str, Any]]] = None,
) -> GovernanceState:
    """Record a validated step in typed ``GovernanceState`` (Phase 1 Ruling B)."""
    base = prior_gov or GovernanceState(
        run_id=str(run_id or ""),
        agent_run_id=agent_run_id,
        table=table,
    )
    steps = list(prior_steps or [])
    steps.append(step.to_dict())
    graph_out = {
        "table": table,
        "node_index": node_index + 1,
        "steps": steps,
        "status": step.status.value,
        "error": step.error or "",
    }
    gov = GovernanceState.from_graph_state(
        graph_out,
        run_id=str(run_id or ""),
        agent_run_id=agent_run_id,
        base=base,
    )
    if agent_run_id:
        lineage.save_governance_state(agent_run_id, gov.to_dict())
    return gov
