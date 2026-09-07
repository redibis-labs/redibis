"""Review queue — steward inbox grouped by proposal type (Phase 5 T5.2)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional
from uuid import uuid4

from redibis.agents.registry import get_node
from redibis.agents.run_models import AgentRun, RunStatus, StepStatus


@dataclass
class ReviewItem:
    """One human judgment proposal."""

    id: str
    kind: str
    table: str
    summary: str
    approval_role: str = "Steward"
    column: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    memory_hint: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"
    auto_pass: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "table": self.table,
            "summary": self.summary,
            "approval_role": self.approval_role,
            "column": self.column,
            "evidence": dict(self.evidence),
            "memory_hint": dict(self.memory_hint),
            "provenance": dict(self.provenance),
            "status": self.status,
            "auto_pass": self.auto_pass,
        }


def _autonomy_for_kind(node_kind: str) -> str:
    try:
        return get_node(node_kind).autonomy_default
    except KeyError:
        return "suggest-only"


def build_review_queue(
    run: AgentRun,
    *,
    memory_retriever: Any = None,
    auto_pass_confident: bool = True,
) -> list[ReviewItem]:
    """Build prioritized review inbox from executed steps."""
    items: list[ReviewItem] = []
    spec_nodes: dict[str, Any] = {}
    for raw in run.pipeline.get("nodes") or []:
        if isinstance(raw, dict):
            spec_nodes[str(raw.get("id") or "")] = raw
        else:
            spec_nodes[raw.id] = raw

    for step in run.steps:
        if step.status not in (StepStatus.COMPLETED, StepStatus.SKIPPED):
            continue
        out = step.output or {}
        node = spec_nodes.get(step.node_id) or {}
        node_kind = step.node_kind or node.get("kind", "")
        autonomy = _autonomy_for_kind(node_kind)
        auto_pass = auto_pass_confident and autonomy == "auto"

        if step.node_kind in ("pii_scan",) or (
            step.node_kind == "contract" and out.get("sub_steps")
        ):
            subs = out.get("sub_steps") or [out]
            for sub in subs:
                if sub.get("kind") != "pii" and step.node_kind == "contract":
                    continue
                detected = int(sub.get("columns_detected") or 0)
                if detected:
                    items.append(ReviewItem(
                        id=uuid4().hex[:10],
                        kind="pii",
                        table=step.table,
                        summary=f"{detected} PII column(s) detected — steward review",
                        approval_role="Steward",
                        evidence={"output": sub},
                        provenance={"step_id": step.step_id, "node_kind": step.node_kind},
                        auto_pass=auto_pass,
                        status="auto_pass" if auto_pass else "pending",
                    ))

        if step.node_kind == "classify" or (
            step.node_kind == "contract"
            and any(s.get("kind") == "classify" for s in (out.get("sub_steps") or []))
        ):
            escalations = out.get("escalations") or []
            tags = out.get("tags") or {}
            for col, tag_list in tags.items():
                items.append(ReviewItem(
                    id=uuid4().hex[:10],
                    kind="classification",
                    table=step.table,
                    column=col,
                    summary=f"Classification tags for {col}: {', '.join(tag_list)}",
                    approval_role="Steward",
                    evidence={"tags": tag_list, "escalations": escalations},
                    provenance={"step_id": step.step_id},
                    auto_pass=auto_pass and not escalations,
                    status="auto_pass" if (auto_pass and not escalations) else "pending",
                ))

        if step.node_kind in ("approval_gate", "gate") and step.status == StepStatus.SKIPPED:
            items.append(ReviewItem(
                id=uuid4().hex[:10],
                kind="approval",
                table=step.table,
                summary=out.get("reason") or "Awaiting human approval",
                approval_role=str(out.get("role") or node.get("params", {}).get("role") or "Steward"),
                evidence=out,
                provenance={"step_id": step.step_id},
                status="pending",
            ))

        if step.node_kind == "enrich" and out.get("valid") is False:
            items.append(ReviewItem(
                id=uuid4().hex[:10],
                kind="enrich",
                table=step.table,
                summary="LLM enrichment produced invalid partial — review required",
                approval_role="Steward",
                evidence=out,
                provenance={"step_id": step.step_id},
                status="pending",
            ))

        if memory_retriever is not None and items:
            _attach_memory_hints(items[-1], memory_retriever)

    items.extend(_hitl_interrupt_items(run))
    return items


def _paused_table(run: AgentRun) -> str:
    meta = run.batch_meta or {}
    paused = str(meta.get("paused_table") or "")
    if paused:
        return paused
    intr = (run.hitl_pending or {}).get("interrupts") or []
    if not intr:
        return ""
    val = intr[0].get("value") if isinstance(intr[0], dict) else {}
    if isinstance(val, dict):
        return str(val.get("table") or "")
    return ""


def _hitl_interrupt_items(run: AgentRun) -> list[ReviewItem]:
    """LangGraph ``interrupt()`` pauses may occur before a step record exists."""
    if run.status != RunStatus.AWAITING_HITL:
        return []
    items: list[ReviewItem] = []
    for intr in (run.hitl_pending or {}).get("interrupts") or []:
        if not isinstance(intr, dict):
            continue
        val = intr.get("value") or {}
        if not isinstance(val, dict):
            continue
        table = str(val.get("table") or _paused_table(run))
        node_kind = str(val.get("node_kind") or "gate")
        items.append(ReviewItem(
            id=str(intr.get("id") or uuid4().hex[:10]),
            kind="approval",
            table=table,
            summary=f"Pipeline paused at {node_kind} — approval required",
            approval_role=str(val.get("role") or "Steward"),
            evidence=dict(val),
            provenance={
                "interrupt_id": intr.get("id", ""),
                "source": "langgraph_interrupt",
                "session_id": run.session_id,
            },
            status="pending",
        ))
    return items


def _attach_memory_hints(item: ReviewItem, retriever: Any) -> None:
    try:
        from redibis.memory.fingerprint import column_fingerprint

        fp = column_fingerprint(
            table=item.table,
            column=item.column or item.table,
            logical_type="",
            tags=[],
        )
        contexts = retriever.retrieve(fp)
        if contexts:
            item.memory_hint = {
                "matches": len(contexts),
                "top_similarity": getattr(contexts[0], "similarity", None),
                "prior_decisions": [
                    d.classification for c in contexts[:1] for d in c.decisions[:3]
                ],
            }
    except Exception:
        pass


def queue_summary(items: list[ReviewItem]) -> dict[str, Any]:
    by_kind: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for item in items:
        by_kind[item.kind] = by_kind.get(item.kind, 0) + 1
        by_status[item.status] = by_status.get(item.status, 0) + 1
    return {
        "total": len(items),
        "pending": by_status.get("pending", 0),
        "auto_pass": by_status.get("auto_pass", 0),
        "by_kind": by_kind,
        "by_status": by_status,
    }
