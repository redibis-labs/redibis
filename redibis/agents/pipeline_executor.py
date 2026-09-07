"""Single-table pipeline executor — LangGraph (default) or batch fallback."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from redibis.agents.batch_executor import BatchExecutor
from redibis.agents.lineage_store import LineageStore
from redibis.agents.models import PipelineSpec
from redibis.agents.run_models import AgentRun
from redibis.agents.tool_runner import ToolContext
from redibis.config import RedibisConfig


def langgraph_available() -> bool:
    try:
        import langgraph  # noqa: F401
        return True
    except ImportError:
        return False


def _use_langgraph_backend(config: RedibisConfig) -> bool:
    mode = (config.agents.single_table_executor or "langgraph").lower()
    if mode == "batch":
        return False
    return langgraph_available()


class PipelineExecutor:
    """Run a pipeline spec for one table synchronously.

    Default backend is ``LangGraphExecutor`` (durable checkpoints + ``interrupt()``).
    Set ``agents.single_table_executor: batch`` or omit ``redibis[agents]`` to fall back
    to the sequential ``BatchExecutor`` loop (non-pausing HITL — see batch_executor doc).
    """

    def __init__(self, lineage: LineageStore, *, tool_ctx: ToolContext):
        self._lineage = lineage
        self._batch = BatchExecutor(lineage, tool_ctx=tool_ctx)

    def execute(
        self,
        spec: PipelineSpec,
        table: str,
        *,
        dry_run: bool = False,
        sample_path: Optional[str] = None,
        use_langgraph: Optional[bool] = None,
        run_id: Optional[str] = None,
    ) -> AgentRun:
        sample_paths = dict(self._batch.tool_ctx.sample_paths)
        if sample_path:
            sample_paths[table] = sample_path
        ctx = ToolContext(
            contract_store=self._batch.tool_ctx.contract_store,
            catalog_service=self._batch.tool_ctx.catalog_service,
            classification_service=self._batch.tool_ctx.classification_service,
            config=self._batch.tool_ctx.config,
            dry_run=dry_run,
            execute=True,
            sample_paths=sample_paths,
            sample_dir=self._batch.tool_ctx.sample_dir,
            sub_store=self._batch.tool_ctx.sub_store,
            run_merger=self._batch.tool_ctx.run_merger,
        )

        cfg = ctx.config if isinstance(ctx.config, RedibisConfig) else RedibisConfig.default()
        prefer_lg = use_langgraph if use_langgraph is not None else _use_langgraph_backend(cfg)

        if prefer_lg:
            try:
                from redibis.agents.executor import LangGraphExecutor

                lg = LangGraphExecutor(self._lineage, tool_ctx=ctx, redibis_config=cfg)
                run, session = lg.execute(spec, table, run_id=run_id)
                run.session_id = session.session_id
                return run
            except ImportError:
                pass

        if use_langgraph is False and isinstance(cfg, RedibisConfig):
            cfg = RedibisConfig.from_dict(cfg.to_dict())
            cfg.agents.batch_executor = "sequential"
            ctx = ToolContext(
                contract_store=ctx.contract_store,
                catalog_service=ctx.catalog_service,
                classification_service=ctx.classification_service,
                config=cfg,
                dry_run=ctx.dry_run,
                execute=ctx.execute,
                sample_paths=ctx.sample_paths,
                sample_dir=ctx.sample_dir,
                sub_store=ctx.sub_store,
                run_merger=ctx.run_merger,
            )

        self._batch.tool_ctx = ctx
        run = self._batch.run(spec, tables=[table], dry_run=dry_run, run_id=run_id)
        return run
