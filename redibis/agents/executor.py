"""LangGraph executor — durable, interruptible pipeline over deterministic tools (Phase 3)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional, TypedDict
from uuid import uuid4

from redibis.agents.cancellation import RunCancelled
from redibis.agents.governance_persist import save_step_governance
from redibis.agents.governance_state import GovernanceState
from redibis.agents.lineage_store import LineageStore
from redibis.agents.models import PipelineNode, PipelineSpec
from redibis.agents.prompt_compiler import compile_prompt_plan, topological_order
from redibis.agents.registry import resolve_node_type, validate_spec
from redibis.agents.run_models import AgentRun, RunStatus, StepRecord, StepStatus
from redibis.agents.scan_bindings import get_table_state
from redibis.agents.session import AgenticSession, save_manifest
from redibis.agents.tool_runner import ToolContext, run_node_step
from redibis.agents.validator import run_step_with_validation
from redibis.config import RedibisConfig
from redibis.telemetry.otel import run_context

_HITL_KINDS = frozenset({"gate", "publish", "approval_gate", "contract_write"})


class _GraphState(TypedDict):
    table: str
    node_index: int
    steps: list[dict[str, Any]]
    status: str
    error: str


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stamp_agent_evidence_status(run_dir, agent_run: AgentRun, table: str) -> None:
    """Record cancelled/failed/success on the agent run evidence manifest."""
    if run_dir is None:
        return
    from pathlib import Path

    from redibis.evidence.manifest import build_manifest
    from redibis.evidence.persist import atomic_write_json

    dest = Path(run_dir) / "evidence_manifest.json"
    status_map = {
        RunStatus.CANCELLED: "cancelled",
        RunStatus.FAILED: "failed",
        RunStatus.COMPLETED: "success",
    }
    run_status = status_map.get(
        agent_run.status,
        str(agent_run.status.value if hasattr(agent_run.status, "value") else agent_run.status),
    )
    payload = None
    if dest.is_file():
        try:
            import json

            payload = json.loads(dest.read_text(encoding="utf-8"))
        except Exception:
            payload = None
    if isinstance(payload, dict):
        payload["run_status"] = run_status
        if agent_run.error:
            warns = list(payload.get("errors") or [])
            warns.append({"phase": "agentic", "error": agent_run.error})
            payload["errors"] = warns
        try:
            atomic_write_json(dest, payload)
        except OSError:
            pass
        return
    try:
        atomic_write_json(dest, build_manifest(
            table=table,
            run_id=agent_run.run_id,
            artifacts={},
            agentic=True,
            run_status=run_status,
        ))
    except OSError:
        pass


def _capture_agent_routing_meta(cfg: Optional[RedibisConfig] = None) -> dict[str, Any]:
    """Freeze capability routing onto AgentRun.batch_meta for new-run binding."""
    try:
        from redibis.config import load_global_settings_optional
        from redibis.enrich.capability_routing import capture_routing_snapshot

        snap = capture_routing_snapshot(
            load_global_settings_optional(),
            agents_cfg=getattr(cfg, "agents", None) if cfg else None,
        )
        return {"capability_routing": snap.to_dict()}
    except Exception:
        return {}


def _requires_hitl(kind: str, params: dict[str, Any], *, auto_approve_writes: bool) -> bool:
    canonical = resolve_node_type(kind)
    if auto_approve_writes:
        return False
    if canonical == "gate" or kind == "approval_gate":
        return not params.get("auto_approve")
    if canonical == "publish" or kind == "catalog_push":
        return not params.get("dry_run")
    if kind == "contract_write":
        return not params.get("auto_approve")
    return canonical in _HITL_KINDS


def _approval_from_resume(resume_value: Any) -> bool:
    if resume_value is True:
        return True
    if isinstance(resume_value, dict):
        return bool(resume_value.get("approved") or resume_value.get("approve"))
    return bool(resume_value)


def _hitl_payload_from_result(result: dict[str, Any]) -> dict[str, Any]:
    """Serialize LangGraph ``__interrupt__`` for API / manifest consumers."""
    raw = result.get("__interrupt__")
    if not raw:
        return {}
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    out: list[dict[str, Any]] = []
    for item in items:
        if hasattr(item, "value"):
            out.append({
                "id": getattr(item, "id", ""),
                "value": item.value,
            })
        elif isinstance(item, dict):
            out.append(item)
        else:
            out.append({"value": item})
    return {"interrupts": out}


class LangGraphExecutor:
    """Compile ``PipelineSpec`` → LangGraph; invoke deterministic tools per node.

    Per-run context (``ToolContext``, cancellation token, session) is passed via the
    ``runtime`` dict given to ``compile()`` — never stored on ``self`` (safe for
    concurrent single-table runs).

    Batch / whole-DB runs use ``BatchExecutor`` with a shared checkpointer from
    ``redibis.agents.checkpointer`` (Postgres when ``memory.enabled``, else per-batch Memory).
    """

    def __init__(
        self,
        lineage: LineageStore,
        *,
        tool_ctx: Optional[ToolContext] = None,
        redibis_config: Optional[RedibisConfig] = None,
        checkpointer: Any = None,
    ):
        self.lineage = lineage
        self.tool_ctx = tool_ctx or ToolContext()
        self.config = redibis_config or RedibisConfig.default()
        self._checkpointer = checkpointer
        self._checkpointer_instance: Any = None

    def _build_checkpointer(self) -> Any:
        if self._checkpointer is not None:
            return self._checkpointer
        if self._checkpointer_instance is not None:
            return self._checkpointer_instance
        from redibis.agents.checkpointer import build_checkpointer

        self._checkpointer_instance = build_checkpointer(self.config)
        return self._checkpointer_instance

    def compile(
        self,
        spec: PipelineSpec,
        *,
        runtime: dict[str, Any],
        validate: bool = True,
    ) -> Any:
        if validate:
            errors = validate_spec(spec)
            if errors:
                raise ValueError(
                    "invalid pipeline spec: " + "; ".join(e.message for e in errors)
                )

        try:
            from langgraph.graph import END, START, StateGraph
            from langgraph.types import interrupt
        except ImportError as exc:
            raise ImportError(
                "LangGraph is required for agent execution — install redibis[agents]"
            ) from exc

        node_by_id = {n.id: n for n in spec.nodes}
        order_ids = topological_order(spec)
        ordered_nodes = [node_by_id[nid] for nid in order_ids if nid in node_by_id]
        cfg = self.config
        lineage = self.lineage

        def _make_step_fn(node: PipelineNode, index: int):
            def _step(state: _GraphState, config: Optional[dict] = None) -> _GraphState:
                ctx: ToolContext = runtime["tool_ctx"]
                token = runtime.get("cancellation_token")
                if token is not None:
                    token.check()

                gov_base = GovernanceState(
                    run_id=str(runtime.get("run_id") or ""),
                    agent_run_id=ctx.agent_run_id,
                    table=state["table"],
                )
                if config and config.get("configurable", {}).get("governance_state"):
                    gov_base = GovernanceState.from_dict(
                        config["configurable"]["governance_state"]
                    )

                if _requires_hitl(
                    node.kind,
                    node.params,
                    auto_approve_writes=cfg.agents.auto_approve_writes,
                ):
                    resume_val = interrupt({
                        "node_id": node.id,
                        "node_kind": node.kind,
                        "table": state["table"],
                        "role": node.params.get("role", "Steward"),
                    })
                    if _approval_from_resume(resume_val):
                        get_table_state(ctx.run_states, state["table"]).approval_granted = True

                with run_context(runtime.get("run_id", "agent")) as tel:
                    with tel.span("agent.langgraph.step", node_kind=node.kind, table=state["table"]):
                        step = run_step_with_validation(node, table=state["table"], ctx=ctx)
                        steps = list(state.get("steps") or [])
                        steps.append(step.to_dict())
                        session: Optional[AgenticSession] = runtime.get("session")
                        run_states = ctx.run_states or {}
                        ts = run_states.get(state["table"])
                        if session and ts and ts.run_id:
                            session.record_table_run(state["table"], ts.run_id)

                graph_out = {
                    "table": state["table"],
                    "node_index": index + 1,
                    "steps": steps,
                    "status": step.status.value,
                    "error": step.error or "",
                }
                save_step_governance(
                    lineage,
                    step=step,
                    table=state["table"],
                    node_index=index,
                    run_id=str(runtime.get("run_id") or ""),
                    agent_run_id=ctx.agent_run_id,
                    prior_gov=gov_base,
                    prior_steps=list(state.get("steps") or []),
                )
                return graph_out

            return _step

        graph = StateGraph(_GraphState)
        if not ordered_nodes:
            raise ValueError("pipeline spec has no nodes")

        first = ordered_nodes[0]
        graph.add_node(first.id, _make_step_fn(first, 0))
        graph.add_edge(START, first.id)

        for i in range(1, len(ordered_nodes)):
            node = ordered_nodes[i]
            prev = ordered_nodes[i - 1]
            graph.add_node(node.id, _make_step_fn(node, i))
            graph.add_edge(prev.id, node.id)

        graph.add_edge(ordered_nodes[-1].id, END)
        return graph.compile(checkpointer=self._build_checkpointer())

    def _build_tool_context(
        self,
        *,
        run_states: Optional[dict] = None,
        cancellation_token: Any = None,
        agent_run_id: str = "",
        capability_routing: Optional[dict[str, Any]] = None,
    ) -> ToolContext:
        routing = capability_routing
        if routing is None:
            routing = getattr(self.tool_ctx, "capability_routing", None) or {}
        return ToolContext(
            contract_store=self.tool_ctx.contract_store,
            catalog_service=self.tool_ctx.catalog_service,
            classification_service=self.tool_ctx.classification_service,
            config=self.tool_ctx.config or self.config,
            dry_run=self.tool_ctx.dry_run,
            execute=self.tool_ctx.execute,
            run_states=run_states if run_states is not None else {},
            sample_paths=dict(self.tool_ctx.sample_paths),
            sample_dir=self.tool_ctx.sample_dir,
            cancellation_token=cancellation_token,
            sub_store=self.tool_ctx.sub_store,
            run_merger=self.tool_ctx.run_merger,
            agent_run_id=agent_run_id,
            capability_routing=routing,
        )

    def _finalize_run(
        self,
        agent_run: AgentRun,
        agent_session: AgenticSession,
        final: dict[str, Any],
    ) -> tuple[AgentRun, AgenticSession]:
        hitl = _hitl_payload_from_result(final)
        if hitl:
            agent_run.steps = [StepRecord.from_dict(s) for s in final.get("steps") or []]
            agent_run.hitl_pending = hitl
            agent_run.status = RunStatus.AWAITING_HITL
            agent_session.status = RunStatus.AWAITING_HITL
            return agent_run, agent_session

        agent_run.steps = [StepRecord.from_dict(s) for s in final.get("steps") or []]
        agent_run.status = RunStatus.COMPLETED
        agent_run.hitl_pending = {}
        agent_session.status = RunStatus.COMPLETED
        return agent_run, agent_session

    def execute(
        self,
        spec: PipelineSpec,
        table: str,
        *,
        session: Optional[AgenticSession] = None,
        thread_id: Optional[str] = None,
        run_id: Optional[str] = None,
        persist_lineage: bool = True,
        cancellation_token: Any = None,
        validate: bool = True,
    ) -> tuple[AgentRun, AgenticSession]:
        """Run a validated spec for one table; return run record + session manifest."""
        plan = compile_prompt_plan(spec)
        agent_session = session or AgenticSession.from_spec(spec, plan)
        agent_session.langgraph_thread_id = thread_id or agent_session.session_id
        agent_session.status = RunStatus.RUNNING
        agent_session.created_at = agent_session.created_at or _utc_iso()

        token = cancellation_token or self.lineage.cancellation_token(agent_session.session_id)

        agent_run = AgentRun(
            run_id=run_id or uuid4().hex[:16],
            name=spec.name,
            status=RunStatus.RUNNING,
            pipeline=spec.to_dict(),
            tables=[table],
            created_at=_utc_iso(),
            session_id=agent_session.session_id,
            batch_meta=_capture_agent_routing_meta(self.config),
        )
        routing = (agent_run.batch_meta or {}).get("capability_routing") or {}
        ctx = self._build_tool_context(
            cancellation_token=token,
            agent_run_id=agent_run.run_id,
            capability_routing=routing if isinstance(routing, dict) else {},
        )
        if persist_lineage:
            self.lineage.save(agent_run)
        save_manifest(self.lineage.root, agent_session)

        runtime: dict[str, Any] = {
            "tool_ctx": ctx,
            "cancellation_token": token,
            "session": agent_session,
            "run_id": agent_run.run_id,
        }
        app = self.compile(spec, runtime=runtime, validate=validate)

        config = {"configurable": {"thread_id": agent_session.langgraph_thread_id}}
        initial: _GraphState = {
            "table": table,
            "node_index": 0,
            "steps": [],
            "status": RunStatus.RUNNING.value,
            "error": "",
        }

        try:
            from redibis.telemetry.llm_evidence import llm_evidence_recorder
            from redibis.telemetry.otel import run_context

            evidence_dir = self.lineage.root / agent_run.run_id if persist_lineage else None
            with run_context(agent_run.run_id) as telemetry:
                with llm_evidence_recorder(
                    run_dir=evidence_dir,
                    run_id=agent_run.run_id,
                    table=table,
                    agent_run_id=agent_run.run_id,
                    execution_mode="agentic",
                    config=self.config,
                ):
                    final = app.invoke(initial, config)
                    agent_run, agent_session = self._finalize_run(agent_run, agent_session, final)
                    agent_run.telemetry = telemetry.export()
        except RunCancelled as exc:
            agent_run.status = RunStatus.CANCELLED
            agent_session.status = RunStatus.CANCELLED
            agent_session.cancel_reason = str(exc)
        except Exception as exc:
            agent_run.status = RunStatus.FAILED
            agent_run.error = str(exc)
            agent_session.status = RunStatus.FAILED
            agent_session.error = str(exc)
        finally:
            if agent_run.status != RunStatus.AWAITING_HITL:
                agent_run.finished_at = _utc_iso()
                agent_session.finished_at = _utc_iso()
            if persist_lineage:
                self.lineage.save(agent_run)
            save_manifest(self.lineage.root, agent_session)
            if persist_lineage:
                evidence_dir = self.lineage.root / agent_run.run_id
                _stamp_agent_evidence_status(evidence_dir, agent_run, table)
            if persist_lineage and agent_run.status in (
                RunStatus.COMPLETED, RunStatus.CANCELLED, RunStatus.FAILED,
            ):
                self.lineage.drop_token(agent_session.session_id)
                from redibis.agents.run_log import drop_run_log

                drop_run_log(agent_run.run_id)

        return agent_run, agent_session

    def resume(
        self,
        spec: PipelineSpec,
        table: str,
        *,
        agent_run: AgentRun,
        agent_session: AgenticSession,
        resume_value: Any,
        persist_lineage: bool = True,
        cancellation_token: Any = None,
    ) -> tuple[AgentRun, AgenticSession]:
        """Continue a run paused at ``interrupt()`` after human approval."""
        if agent_run.status != RunStatus.AWAITING_HITL:
            raise ValueError(f"run {agent_run.run_id!r} is not awaiting HITL")

        try:
            from langgraph.types import Command
        except ImportError as exc:
            raise ImportError(
                "LangGraph is required for agent resume — install redibis[agents]"
            ) from exc

        token = cancellation_token or self.lineage.cancellation_token(agent_session.session_id)
        ctx = self._build_tool_context(
            cancellation_token=token,
            agent_run_id=agent_run.run_id,
        )
        if _approval_from_resume(resume_value):
            get_table_state(ctx.run_states, table).approval_granted = True

        runtime: dict[str, Any] = {
            "tool_ctx": ctx,
            "cancellation_token": token,
            "session": agent_session,
            "run_id": agent_run.run_id,
        }
        app = self.compile(spec, runtime=runtime, validate=False)
        config = {"configurable": {"thread_id": agent_session.langgraph_thread_id}}

        agent_run.status = RunStatus.RUNNING
        agent_session.status = RunStatus.RUNNING
        agent_run.hitl_pending = {}
        if persist_lineage:
            self.lineage.save(agent_run)
        save_manifest(self.lineage.root, agent_session)

        try:
            from redibis.telemetry.llm_evidence import llm_evidence_recorder
            from redibis.telemetry.otel import run_context

            evidence_dir = self.lineage.root / agent_run.run_id if persist_lineage else None
            with run_context(agent_run.run_id) as telemetry:
                with llm_evidence_recorder(
                    run_dir=evidence_dir,
                    run_id=agent_run.run_id,
                    table=table,
                    agent_run_id=agent_run.run_id,
                    execution_mode="agentic",
                    config=self.config,
                ):
                    final = app.invoke(Command(resume=resume_value), config)
                    agent_run, agent_session = self._finalize_run(agent_run, agent_session, final)
                    agent_run.telemetry = telemetry.export() or list(agent_run.telemetry or [])
        except RunCancelled as exc:
            agent_run.status = RunStatus.CANCELLED
            agent_session.status = RunStatus.CANCELLED
            agent_session.cancel_reason = str(exc)
        except Exception as exc:
            agent_run.status = RunStatus.FAILED
            agent_run.error = str(exc)
            agent_session.status = RunStatus.FAILED
            agent_session.error = str(exc)
        finally:
            if agent_run.status != RunStatus.AWAITING_HITL:
                agent_run.finished_at = _utc_iso()
                agent_session.finished_at = _utc_iso()
            if persist_lineage:
                self.lineage.save(agent_run)
            save_manifest(self.lineage.root, agent_session)
            if persist_lineage:
                evidence_dir = self.lineage.root / agent_run.run_id
                _stamp_agent_evidence_status(evidence_dir, agent_run, table)
            if persist_lineage and agent_run.status in (
                RunStatus.COMPLETED, RunStatus.CANCELLED, RunStatus.FAILED,
            ):
                self.lineage.drop_token(agent_session.session_id)
                from redibis.agents.run_log import drop_run_log

                drop_run_log(agent_run.run_id)

        return agent_run, agent_session
