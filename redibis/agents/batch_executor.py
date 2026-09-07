"""Batch executor — fan-out pipeline over tables with cooperative cancellation.

Default backend (``agents.batch_executor: langgraph``) runs each table through
``LangGraphExecutor`` — durable checkpoints + real ``interrupt()`` pauses.

Legacy ``sequential`` mode keeps the per-node loop (non-pausing HITL; writes blocked
by ``approval_granted`` downstream).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from redibis.agents.cancellation import RunCancelled
from redibis.agents.governance_persist import save_step_governance
from redibis.agents.governance_state import GovernanceState
from redibis.agents.lineage_store import LineageStore
from redibis.agents.models import PipelineSpec
from redibis.agents.prompt_compiler import topological_order
from redibis.agents.run_models import (
    AgentRun,
    RunStatus,
    StepRecord,
    StepStatus,
    TaskEntry,
    TaskLedger,
    TaskStatus,
)
from redibis.agents.tool_runner import ToolContext, run_node_step
from redibis.agents.validator import run_step_with_validation
from redibis.config import RedibisConfig
from redibis.agents.checkpointer import build_checkpointer, drop_batch_checkpointer
from redibis.telemetry.otel import run_context


def _batch_checkpointer(batch_run_id: str, cfg: RedibisConfig) -> Any:
    return build_checkpointer(cfg, batch_run_id=batch_run_id)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redibis_config(ctx: ToolContext) -> RedibisConfig:
    if isinstance(ctx.config, RedibisConfig):
        return ctx.config
    return RedibisConfig.default()


def _use_langgraph_batch(cfg: RedibisConfig) -> bool:
    from redibis.agents.pipeline_executor import langgraph_available

    mode = (cfg.agents.batch_executor or "langgraph").lower()
    if mode == "sequential":
        return False
    return langgraph_available()


class BatchExecutor:
    """Orchestrate a pipeline across many tables with lineage + OTel."""

    def __init__(self, store: LineageStore, *, tool_ctx: Optional[ToolContext] = None):
        self.store = store
        self.tool_ctx = tool_ctx or ToolContext()

    def _langgraph(
        self,
        cfg: RedibisConfig,
        ctx: ToolContext,
        batch_run_id: str,
    ):
        """One LangGraph executor + checkpointer per batch run (resume needs shared state)."""
        from redibis.agents.executor import LangGraphExecutor

        return LangGraphExecutor(
            self.store,
            tool_ctx=ctx,
            redibis_config=cfg,
            checkpointer=_batch_checkpointer(batch_run_id, cfg),
        )

    def plan_tasks(self, spec: PipelineSpec, tables: list[str]) -> TaskLedger:
        """Build task ledger: each table × each executable node (excl. batch_loop)."""
        node_by_id = {n.id: n for n in spec.nodes}
        order = topological_order(spec)
        ledger = TaskLedger()
        for table in tables:
            for nid in order:
                node = node_by_id.get(nid)
                if not node or node.kind in ("batch_loop",):
                    continue
                ledger.tasks.append(TaskEntry(
                    task_id=uuid4().hex[:10],
                    table=table,
                    node_id=node.id,
                    node_kind=node.kind,
                ))
        return ledger

    def resolve_tables(
        self,
        spec: PipelineSpec,
        *,
        tables: Optional[list[str]] = None,
        database: str = "",
        contract_store: Any = None,
    ) -> list[str]:
        if tables:
            return list(tables)

        loop_node = next((n for n in spec.nodes if n.kind == "batch_loop"), None)
        db_filter = database or (loop_node.params.get("database") if loop_node else "") or ""

        if contract_store is not None:
            listed = contract_store.list_tables()
            if db_filter:
                prefix = f"{db_filter}."
                listed = [t for t in listed if t.startswith(prefix)]
            if listed:
                return sorted(listed)

        source = next((n for n in spec.nodes if n.kind == "source_table"), None)
        if source and source.params.get("table"):
            return [str(source.params["table"])]

        return []

    def run(
        self,
        spec: PipelineSpec,
        *,
        tables: Optional[list[str]] = None,
        database: str = "",
        dry_run: bool = False,
        run_id: Optional[str] = None,
        resume: bool = False,
    ) -> AgentRun:
        resolved = self.resolve_tables(
            spec,
            tables=tables,
            database=database,
            contract_store=self.tool_ctx.contract_store,
        )
        if not resolved:
            raise ValueError("no tables to process — pass --tables or ensure contract store has actives")

        cfg = _redibis_config(self.tool_ctx)
        if resume:
            if not run_id:
                raise ValueError("run_id required for batch resume")
            return self._resume_langgraph_batch(spec, run_id=run_id, dry_run=dry_run)

        if _use_langgraph_batch(cfg):
            return self._run_langgraph_batch(
                spec,
                tables=resolved,
                dry_run=dry_run,
                run_id=run_id,
            )
        return self._run_sequential(
            spec,
            tables=resolved,
            dry_run=dry_run,
            run_id=run_id,
        )

    def resume_batch(
        self,
        spec: PipelineSpec,
        run_id: str,
        *,
        resume_value: Any,
        dry_run: bool = False,
    ) -> AgentRun:
        """Resume a LangGraph batch paused at HITL and continue remaining tables."""
        from redibis.agents.session import load_manifest

        agent_run = self.store.load(run_id)
        if agent_run.status != RunStatus.AWAITING_HITL:
            raise ValueError(f"batch run {run_id!r} is not awaiting HITL")

        meta = agent_run.batch_meta or {}
        table = str(meta.get("paused_table") or "")
        session_id = agent_run.session_id or str(meta.get("session_id") or "")
        if not table or not session_id:
            raise ValueError("batch run missing paused_table or session_id in batch_meta")

        cfg = _redibis_config(self.tool_ctx)
        token = self.store.cancellation_token(agent_run.run_id)
        ctx = self._build_context(
            dry_run=dry_run,
            token=token,
            agent_run_id=agent_run.run_id,
        )
        session = load_manifest(self.store.root, session_id)

        table_steps = [s for s in agent_run.steps if s.table == table]
        ephemeral = AgentRun(
            run_id=agent_run.run_id,
            name=agent_run.name,
            status=RunStatus.AWAITING_HITL,
            pipeline=agent_run.pipeline,
            tables=[table],
            steps=table_steps,
            hitl_pending=agent_run.hitl_pending,
            session_id=session_id,
        )

        lg = self._langgraph(cfg, ctx, agent_run.run_id)
        table_run, session = lg.resume(
            spec,
            table,
            agent_run=ephemeral,
            agent_session=session,
            resume_value=resume_value,
            persist_lineage=False,
            cancellation_token=token,
        )

        agent_run.steps = [s for s in agent_run.steps if s.table != table] + table_run.steps
        self._sync_ledger_for_table(agent_run.ledger, table, table_run.steps)

        if table_run.status == RunStatus.AWAITING_HITL:
            agent_run.status = RunStatus.AWAITING_HITL
            agent_run.hitl_pending = table_run.hitl_pending
            agent_run.session_id = session.session_id
            agent_run.batch_meta = {
                "executor": "langgraph",
                "paused_table": table,
                "session_id": session.session_id,
            }
            self.store.save(agent_run)
            return agent_run

        if table_run.status == RunStatus.FAILED:
            agent_run.status = RunStatus.FAILED
            agent_run.error = table_run.error
            agent_run.finished_at = _utc_iso()
            self.store.save(agent_run)
            self.store.drop_token(agent_run.run_id)
            return agent_run

        return self._continue_langgraph_batch(
            spec,
            agent_run,
            start_after_table=table,
            ctx=ctx,
            dry_run=dry_run,
            token=token,
        )

    def _resume_langgraph_batch(
        self,
        spec: PipelineSpec,
        *,
        run_id: str,
        dry_run: bool,
    ) -> AgentRun:
        """Continue a batch run from the last completed table (no HITL resume value).

        Requires a **durable** checkpointer (``memory.enabled`` + Postgres DSN) for
        process-crash recovery — per-batch ``MemorySaver`` is lost when the worker dies.

        Skips tables whose ledger tasks are all DONE. Tables with partial ledger/step
        state but no ``last_completed_table`` marker are re-entered via ``execute()``
        on the existing ``thread_id``; if a LangGraph checkpoint already exists for
        that thread, behavior depends on LangGraph semantics (see
        ``test_langgraph_batch_crash_resume_mid_table_edge``).
        """
        agent_run = self.store.load(run_id)
        meta = agent_run.batch_meta or {}
        last = str(meta.get("last_completed_table") or "")
        ctx = self._build_context(
            dry_run=dry_run,
            token=self.store.cancellation_token(agent_run.run_id),
            agent_run_id=agent_run.run_id,
        )
        return self._continue_langgraph_batch(
            spec,
            agent_run,
            start_after_table=last,
            ctx=ctx,
            dry_run=dry_run,
            token=self.store.cancellation_token(agent_run.run_id),
        )

    def _run_langgraph_batch(
        self,
        spec: PipelineSpec,
        *,
        tables: list[str],
        dry_run: bool,
        run_id: Optional[str],
    ) -> AgentRun:
        agent_run = AgentRun(
            name=spec.name,
            status=RunStatus.RUNNING,
            pipeline=spec.to_dict(),
            tables=tables,
            ledger=self.plan_tasks(spec, tables),
            created_at=_utc_iso(),
            batch_meta={"executor": "langgraph"},
        )
        if run_id:
            agent_run.run_id = run_id
        self.store.save(agent_run)
        token = self.store.cancellation_token(agent_run.run_id)
        ctx = self._build_context(
            dry_run=dry_run,
            token=token,
            agent_run_id=agent_run.run_id,
        )
        return self._continue_langgraph_batch(
            spec,
            agent_run,
            start_after_table="",
            ctx=ctx,
            dry_run=dry_run,
            token=token,
        )

    def _continue_langgraph_batch(
        self,
        spec: PipelineSpec,
        agent_run: AgentRun,
        *,
        start_after_table: str,
        ctx: ToolContext,
        dry_run: bool,
        token: Any,
    ) -> AgentRun:
        cfg = _redibis_config(ctx)
        lg = self._langgraph(cfg, ctx, agent_run.run_id)
        tables = list(agent_run.tables)
        if start_after_table and start_after_table in tables:
            idx = tables.index(start_after_table)
            tables = tables[idx + 1:]

        agent_run.status = RunStatus.RUNNING
        agent_run.hitl_pending = {}
        self.store.save(agent_run)

        try:
            with run_context(agent_run.run_id) as telemetry:
                for table in tables:
                    if self._table_tasks_done(agent_run.ledger, table):
                        continue
                    token.check()

                    thread_id = f"{agent_run.run_id}:{table}"
                    validate = (
                        start_after_table == ""
                        and table == tables[0]
                        and not any(s.table == table for s in agent_run.steps)
                    )
                    table_run, session = lg.execute(
                        spec,
                        table,
                        thread_id=thread_id,
                        persist_lineage=False,
                        cancellation_token=token,
                        validate=validate,
                    )

                    agent_run.steps = [
                        s for s in agent_run.steps if s.table != table
                    ] + table_run.steps
                    self._sync_ledger_for_table(agent_run.ledger, table, table_run.steps)
                    agent_run.session_id = session.session_id
                    agent_run.batch_meta = {
                        "executor": "langgraph",
                        "session_id": session.session_id,
                    }

                    if table_run.status == RunStatus.AWAITING_HITL:
                        agent_run.status = RunStatus.AWAITING_HITL
                        agent_run.hitl_pending = table_run.hitl_pending
                        agent_run.batch_meta["paused_table"] = table
                        self.store.save(agent_run)
                        return agent_run

                    agent_run.batch_meta["last_completed_table"] = table

                    if table_run.status == RunStatus.FAILED:
                        agent_run.status = RunStatus.FAILED
                        agent_run.error = table_run.error
                        break

                    self.store.save(agent_run)

                if agent_run.status == RunStatus.RUNNING:
                    agent_run.telemetry = telemetry.export()
                    agent_run.status = RunStatus.COMPLETED
                    agent_run.finished_at = _utc_iso()
        except RunCancelled as exc:
            agent_run.status = RunStatus.CANCELLED
            agent_run.cancel_reason = str(exc)
            self._cancel_pending_tasks(agent_run.ledger)
        except Exception as exc:
            agent_run.status = RunStatus.FAILED
            agent_run.error = str(exc)
        finally:
            if agent_run.status != RunStatus.AWAITING_HITL:
                agent_run.finished_at = agent_run.finished_at or _utc_iso()
            self.store.save(agent_run)
            if agent_run.status in (RunStatus.COMPLETED, RunStatus.CANCELLED, RunStatus.FAILED):
                self.store.drop_token(agent_run.run_id)
                drop_batch_checkpointer(agent_run.run_id)
                from redibis.agents.run_log import drop_run_log

                drop_run_log(agent_run.run_id)

        return agent_run

    def _run_sequential(
        self,
        spec: PipelineSpec,
        *,
        tables: list[str],
        dry_run: bool,
        run_id: Optional[str],
    ) -> AgentRun:
        agent_run = AgentRun(
            name=spec.name,
            status=RunStatus.RUNNING,
            pipeline=spec.to_dict(),
            tables=tables,
            ledger=self.plan_tasks(spec, tables),
            created_at=_utc_iso(),
            batch_meta={"executor": "sequential"},
        )
        if run_id:
            agent_run.run_id = run_id
        self.store.save(agent_run)
        token = self.store.cancellation_token(agent_run.run_id)
        ctx = self._build_context(
            dry_run=dry_run,
            token=token,
            agent_run_id=agent_run.run_id,
        )

        node_by_id = {n.id: n for n in spec.nodes}
        order = topological_order(spec)

        try:
            with run_context(agent_run.run_id) as telemetry:
                for table in tables:
                    token.check()
                    gov_base: GovernanceState | None = None
                    table_steps: list[dict] = []
                    node_index = 0
                    for nid in order:
                        token.check()
                        node = node_by_id.get(nid)
                        if not node or node.kind == "batch_loop":
                            continue

                        task = self._find_task(agent_run.ledger, table, nid)
                        if task:
                            task.status = TaskStatus.RUNNING
                            self.store.save(agent_run)

                        with telemetry.span(
                            "agent.step",
                            table=table,
                            node_kind=node.kind,
                            tool=get_node_tool(node),
                        ) as span:
                            step = run_step_with_validation(node, table=table, ctx=ctx)
                            step.span_id = span.span_id
                            agent_run.steps.append(step)

                            gov_base = save_step_governance(
                                self.store,
                                step=step,
                                table=table,
                                node_index=node_index,
                                run_id=agent_run.run_id,
                                agent_run_id=ctx.agent_run_id,
                                prior_gov=gov_base,
                                prior_steps=table_steps,
                            )
                            table_steps.append(step.to_dict())
                            node_index += 1

                            if task:
                                task.step_id = step.step_id
                                if step.status == StepStatus.COMPLETED:
                                    task.status = TaskStatus.DONE
                                elif step.status == StepStatus.SKIPPED:
                                    task.status = TaskStatus.SKIPPED
                                elif step.status == StepStatus.FAILED:
                                    task.status = TaskStatus.FAILED

                        self.store.save(agent_run)

                agent_run.telemetry = telemetry.export()
                agent_run.status = RunStatus.COMPLETED
        except RunCancelled as exc:
            agent_run.status = RunStatus.CANCELLED
            agent_run.cancel_reason = str(exc)
            self._cancel_pending_tasks(agent_run.ledger)
        except Exception as exc:
            agent_run.status = RunStatus.FAILED
            agent_run.error = str(exc)
        finally:
            agent_run.finished_at = _utc_iso()
            self.store.save(agent_run)
            self.store.drop_token(agent_run.run_id)
            from redibis.agents.run_log import drop_run_log

            drop_run_log(agent_run.run_id)

        return agent_run

    def _build_context(
        self,
        *,
        dry_run: bool,
        token: Any,
        agent_run_id: str = "",
    ) -> ToolContext:
        return ToolContext(
            contract_store=self.tool_ctx.contract_store,
            catalog_service=self.tool_ctx.catalog_service,
            classification_service=self.tool_ctx.classification_service,
            config=self.tool_ctx.config,
            dry_run=dry_run,
            execute=self.tool_ctx.execute,
            run_states={},
            sample_paths=dict(self.tool_ctx.sample_paths),
            sample_dir=self.tool_ctx.sample_dir,
            cancellation_token=token,
            sub_store=self.tool_ctx.sub_store,
            run_merger=self.tool_ctx.run_merger,
            agent_run_id=agent_run_id,
        )

    @staticmethod
    def _find_task(ledger: TaskLedger, table: str, node_id: str) -> Optional[TaskEntry]:
        for task in ledger.tasks:
            if task.table == table and task.node_id == node_id:
                return task
        return None

    @staticmethod
    def _table_tasks_done(ledger: TaskLedger, table: str) -> bool:
        tasks = [t for t in ledger.tasks if t.table == table]
        if not tasks:
            return False
        return all(t.status in (TaskStatus.DONE, TaskStatus.SKIPPED) for t in tasks)

    @staticmethod
    def _sync_ledger_for_table(
        ledger: TaskLedger,
        table: str,
        steps: list[StepRecord],
    ) -> None:
        by_kind: dict[str, StepRecord] = {}
        for step in steps:
            by_kind[step.node_kind] = step
        for task in ledger.tasks:
            if task.table != table:
                continue
            step = by_kind.get(task.node_kind)
            if not step:
                continue
            task.step_id = step.step_id
            if step.status == StepStatus.COMPLETED:
                task.status = TaskStatus.DONE
            elif step.status == StepStatus.SKIPPED:
                task.status = TaskStatus.SKIPPED
            elif step.status == StepStatus.FAILED:
                task.status = TaskStatus.FAILED

    @staticmethod
    def _cancel_pending_tasks(ledger: TaskLedger) -> None:
        for task in ledger.tasks:
            if task.status in (TaskStatus.PLANNED, TaskStatus.RUNNING):
                task.status = TaskStatus.CANCELLED


def get_node_tool(node) -> str:
    from redibis.agents.node_registry import NODE_REGISTRY
    spec = NODE_REGISTRY.get(node.kind)
    return spec.tool if spec else ""
