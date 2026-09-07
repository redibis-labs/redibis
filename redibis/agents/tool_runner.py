"""Execute bound deterministic tools for pipeline node kinds (Phase 5 in-app)."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any, Iterator, Optional
from uuid import uuid4

from redibis.agents.models import PipelineNode
from redibis.agents.node_registry import get_node
from redibis.agents.registry import KIND_ALIASES, resolve_node_type
from redibis.agents.run_models import StepRecord, StepStatus
from redibis.agents.run_state import TableRunState
from redibis.agents.sample_loader import load_sample_dataframe, resolve_sample_path
from redibis.agents.scan_bindings import build_scan_config, get_table_state, new_run_id
from redibis.config import RedibisConfig
from redibis.scan.contract_writer import ScanContractWriter
from redibis.scan.pii_phase import build_pii_contract
from redibis.scan.quality_phase import run_quality_phase
from redibis.services import pipeline as scan_pipeline
from redibis.store.run_merger import RunMerger


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_GATE_KINDS = frozenset({"approval_gate", "gate"})


def _normalize_for_execution(node: PipelineNode) -> PipelineNode:
    """Map canonical registry kinds to legacy tool runners where needed."""
    if node.kind in KIND_ALIASES:
        return node
    canonical = resolve_node_type(node.kind)
    if canonical in ("contract", "mask"):
        return node
    mapping = {
        "source": "source_table",
        "sample": "source_table",
        "profile": "profile_scan",
        "publish": "catalog_push",
        "gate": "approval_gate",
        "batch": "batch_loop",
    }
    legacy = mapping.get(canonical)
    if not legacy or legacy == node.kind:
        return node
    params = dict(node.params)
    if canonical == "sample":
        params.setdefault("sampling_tier", "sample")
    return PipelineNode(
        id=node.id,
        kind=legacy,
        label=node.label,
        params=params,
        position=node.position,
    )


class ToolContext:
    """Dependencies injected into tool runners."""

    def __init__(
        self,
        *,
        contract_store: Any = None,
        catalog_service: Any = None,
        classification_service: Any = None,
        config: Any = None,
        dry_run: bool = False,
        execute: bool = True,
        run_states: Optional[dict[str, TableRunState]] = None,
        sample_paths: Optional[dict[str, str]] = None,
        sample_dir: Optional[Path] = None,
        cancellation_token: Any = None,
        sub_store: Any = None,
        run_merger: Any = None,
        source_config: Any = None,
        spark: Any = None,
        agent_run_id: str = "",
        capability_routing: Optional[dict[str, Any]] = None,
    ):
        self.contract_store = contract_store
        self.catalog_service = catalog_service
        self.classification_service = classification_service
        self.config = config or RedibisConfig.default()
        self.dry_run = dry_run
        self.execute = execute
        self.run_states = run_states if run_states is not None else {}
        self.sample_paths = sample_paths or {}
        self.sample_dir = sample_dir
        self.cancellation_token = cancellation_token
        self.sub_store = sub_store
        self.run_merger = run_merger
        # External source for live sampling (C3b). Defaults to RedibisConfig.source when a
        # config is supplied, so Oracle/Postgres runs activate without extra wiring; Hive
        # additionally needs an injected Spark session.
        self.source_config = source_config or getattr(self.config, "source", None)
        self.spark = spark
        self.agent_run_id = agent_run_id or ""
        self.capability_routing = dict(capability_routing or {})
    def _check_cancel(self) -> None:
        if self.cancellation_token is not None:
            self.cancellation_token.check()

    def _redibis_config(self) -> RedibisConfig:
        if isinstance(self.config, RedibisConfig):
            return self.config
        return RedibisConfig.default()


def _node_tool(node: PipelineNode) -> str:
    try:
        return get_node(node.kind).tool
    except KeyError:
        from redibis.agents.registry import get_node as get_registry_node
        return get_registry_node(node.kind).tool_ref


def _publish_agent_log(ctx: ToolContext, line: str) -> None:
    if not ctx.agent_run_id:
        return
    from pathlib import Path

    from redibis.agents.run_log import append_run_log

    lineage_root = None
    runs_dir = getattr(getattr(ctx.config, "agents", None), "runs_dir", None)
    if runs_dir:
        lineage_root = Path(runs_dir)
    append_run_log(ctx.agent_run_id, line, lineage_root=lineage_root)


def _flush_capture_buffer(ctx: ToolContext, buf: StringIO) -> None:
    if buf is None:
        return
    buf.seek(0)
    for line in buf.read().splitlines():
        if line.strip():
            _publish_agent_log(ctx, line)


@contextmanager
def _step_log_capture(
    ctx: ToolContext,
    *,
    table: str,
    scan_run_id: str,
) -> Iterator[StringIO]:
    """Capture root-logger output for one agent step (SSE + optional disk)."""
    cfg = ctx._redibis_config()
    run_dir = Path(cfg.report.output_dir) / scan_run_id
    if cfg.observability.persist_run_log:
        from redibis.obs import persist_run_log

        with persist_run_log(run_dir, table, scan_run_id) as buf:
            yield buf
        return
    from redibis.services.run_logging import capture_run_log

    with capture_run_log() as buf:
        yield buf


def _format_step_log_line(step: StepRecord) -> str:
    status = step.status.value if hasattr(step.status, "value") else str(step.status)
    line = f"{step.table or '-'} · {step.node_kind} — {status}"
    out = step.output or {}
    if step.error:
        line += f"  ERROR {step.error}"
    elif isinstance(out, dict) and out.get("skipped") and out.get("reason"):
        line += f"  skipped — {out['reason']}"
    elif isinstance(out, dict) and out.get("sub_steps"):
        parts = []
        for sub in out["sub_steps"]:
            kind = sub.get("kind", "?")
            if sub.get("error"):
                parts.append(f"{kind}: ERROR {sub['error']}")
            elif sub.get("skipped"):
                parts.append(f"{kind}: skipped — {sub.get('reason', '')}")
            elif kind == "quality":
                parts.append(
                    f"quality: {sub.get('expectations', 0)} rules · "
                    f"{sub.get('passed', 0)} passed"
                )
            elif kind == "pii":
                parts.append(
                    f"pii: {sub.get('columns_scanned', 0)} cols · "
                    f"{sub.get('columns_detected', 0)} detected"
                )
            elif kind == "classify":
                parts.append(f"classify: {sub.get('columns', 0)} columns tagged")
            elif kind == "enrich":
                parts.append(
                    f"enrich: valid={sub.get('valid')} "
                    f"warnings={sub.get('warnings', 0)}"
                )
            else:
                parts.append(kind)
        if parts:
            line += "\n  " + "\n  ".join(parts)
    return line


def run_node_step(
    node: PipelineNode,
    *,
    table: str,
    ctx: ToolContext,
) -> StepRecord:
    """Execute one pipeline node for a table (deterministic tools only)."""
    from redibis.obs import bind_context

    node = _normalize_for_execution(node)
    tool_name = _node_tool(node)
    step = StepRecord(
        step_id=uuid4().hex[:12],
        node_id=node.id,
        node_kind=node.kind,
        tool=tool_name,
        table=table,
        status=StepStatus.RUNNING,
        started_at=_utc_iso(),
    )

    state = get_table_state(ctx.run_states, table)
    run_id = state.run_id or new_run_id()
    log_buf: Optional[StringIO] = None

    try:
        with _step_log_capture(ctx, table=table, scan_run_id=run_id) as log_buf:
            with bind_context(run_id=run_id, table=table, fn=tool_name):
                ctx._check_cancel()
                if node.kind in _GATE_KINDS:
                    step.output = _run_approval_gate(node, table, ctx)
                    step.status = StepStatus.SKIPPED
                elif node.kind == "source_table":
                    step.output = _run_source_table(node, table, ctx)
                    step.status = StepStatus.COMPLETED
                elif node.kind == "batch_loop":
                    step.output = {"database": node.params.get("database", "")}
                    step.status = StepStatus.COMPLETED
                elif node.kind == "profile_scan":
                    step.output = _run_profile_scan(node, table, ctx)
                    step.status = _status_from_output(step.output)
                elif node.kind == "quality_scan":
                    step.output = _run_quality_scan(node, table, ctx)
                    step.status = _status_from_output(step.output)
                elif node.kind == "pii_scan":
                    step.output = _run_pii_scan(node, table, ctx)
                    step.status = _status_from_output(step.output)
                elif node.kind == "classify":
                    step.output = _run_classify(node, table, ctx)
                    step.status = _status_from_output(step.output)
                elif node.kind == "contract":
                    step.output = _run_contract(node, table, ctx)
                    step.status = _status_from_output(step.output)
                elif node.kind == "mask":
                    step.output = {"skipped": True, "reason": "mask is suggest-only — open masking page"}
                    step.status = StepStatus.SKIPPED
                elif node.kind == "enrich":
                    step.output = _run_enrich(node, table, ctx)
                    step.status = _status_from_output(step.output)
                elif node.kind == "contract_synthesis":
                    step.output = _run_contract_synthesis(node, table, ctx)
                    step.status = _status_from_output(step.output)
                elif node.kind == "catalog_push":
                    step.output = _run_catalog_push(node, table, ctx)
                    step.status = StepStatus.SKIPPED if step.output.get("skipped") else StepStatus.COMPLETED
                elif node.kind == "contract_write":
                    step.output = _run_contract_write(node, table, ctx)
                    step.status = _status_from_output(step.output)
                elif node.kind == "deep_scan":
                    step.output = _run_deep_scan(node, table, ctx)
                    step.status = _status_from_output(step.output)
                elif node.kind == "pii_tune":
                    step.output = _run_pii_tune(node, table, ctx)
                    step.status = _status_from_output(step.output)
                else:
                    from redibis.agents.plugins import get_plugin_runner
                    runner = get_plugin_runner(node.kind)
                    if runner is not None:
                        step.output = runner(node.params, table=table, context=ctx)
                        step.status = _status_from_output(step.output)
                    else:
                        step.status = StepStatus.SKIPPED
                        step.output = {"reason": f"no runner for kind {node.kind!r}"}
    except Exception as exc:
        step.status = StepStatus.FAILED
        step.error = str(exc)
    finally:
        step.finished_at = _utc_iso()
        _flush_capture_buffer(ctx, log_buf)
        _publish_agent_log(ctx, _format_step_log_line(step))
        _emit_step_audit_event(ctx, step, node)

    return step


def _emit_step_audit_event(
    ctx: ToolContext,
    step: StepRecord,
    node: PipelineNode,
) -> None:
    """Append immutable audit event for this node transition."""
    agent_run_id = ctx.agent_run_id
    if not agent_run_id:
        return
    from redibis.agents.governance_state import stable_hash
    from redibis.agents.run_log import append_audit_event

    out = step.output or {}
    actor = "human" if (
        step.node_kind in _GATE_KINDS
        and step.status == StepStatus.SKIPPED
        and "approval" in str(out.get("reason", "")).lower()
    ) else "agent"
    validation: dict[str, Any] = {}
    if step.validation:
        validation = dict(step.validation)
    elif step.node_kind == "enrich" and isinstance(out, dict):
        validation = {
            "valid": out.get("valid"),
            "enrichment_status": out.get("enrichment_status"),
            "warnings": out.get("warnings"),
        }
    append_audit_event(
        agent_run_id,
        table=step.table,
        node=step.node_kind or node.kind,
        actor=actor,
        inputs_hash=stable_hash({"node_id": node.id, "params": node.params, "table": step.table}),
        outputs_hash=stable_hash(out),
        status=step.status.value,
        attempts=step.attempts,
        validation=validation,
        decision=str(out.get("reason") or out.get("skipped") or ""),
    )


def _status_from_output(output: dict[str, Any]) -> StepStatus:
    if output.get("skipped"):
        return StepStatus.SKIPPED
    if output.get("failed"):
        return StepStatus.FAILED
    return StepStatus.COMPLETED


def _run_source_table(node: PipelineNode, table: str, ctx: ToolContext) -> dict[str, Any]:
    state = get_table_state(ctx.run_states, table)
    path = resolve_sample_path(
        table,
        sample_paths=ctx.sample_paths,
        sample_dir=ctx.sample_dir,
        node_params=node.params,
    )
    out: dict[str, Any] = {"table": table, "resolved": True}
    if path and ctx.execute:
        state.df = load_sample_dataframe(path)
        out["sample_path"] = str(path)
        out["rows"] = len(state.df)
    elif path:
        out["sample_path"] = str(path)
    else:
        out["sample_missing"] = True
    return out


def _ensure_dataframe(
    node: PipelineNode,
    table: str,
    ctx: ToolContext,
) -> tuple[TableRunState, Optional[dict[str, Any]]]:
    state = get_table_state(ctx.run_states, table)
    if state.df is not None:
        return state, None
    path = resolve_sample_path(
        table,
        sample_paths=ctx.sample_paths,
        sample_dir=ctx.sample_dir,
        node_params=node.params,
    )
    if path is None:
        sc = getattr(ctx, "source_config", None)
        if sc is not None and (getattr(sc, "engine", "none") or "none").lower() not in ("none", "local"):
            if not ctx.execute:
                return state, {"skipped": True, "reason": "execute disabled (external source)"}
            try:
                from redibis.agents.external_sample import load_external_sample
                rows = int(node.params.get("sample_rows") or node.params.get("rows") or 5000)
                state.df = load_external_sample(sc, table, rows=rows, spark=getattr(ctx, "spark", None))
                return state, None
            except Exception as exc:
                return state, {"skipped": True, "reason": f"external sample failed: {exc}"}
        return state, {
            "skipped": True,
            "reason": "no sample data — set sample_path on source node, --sample-dir, or sample_paths",
        }
    if not ctx.execute:
        return state, {"skipped": True, "reason": "execute disabled", "sample_path": str(path)}
    state.df = load_sample_dataframe(path)
    return state, None


def _persist_run_sample(state: TableRunState, cfg: Any) -> None:
    """Write the in-memory sample to ``<output_dir>/<run_id>/sample.csv``.

    Mirrors the manual session's ``data.csv`` so a handoff to the 360° console has a
    real data file (without this the manual session loads with no data and crashes).
    Best-effort; never raises into the scan.
    """
    try:
        if state.df is None or not state.run_id:
            return
        run_dir = Path(getattr(cfg, "output_dir", "")) / state.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        dest = run_dir / "sample.csv"
        if not dest.exists():
            state.df.to_csv(dest, index=False)
    except Exception:
        pass


def _subset_dataframe(df, node_params: dict[str, Any], *, phase: str):
    """Return a bounded view of ``df`` for PII vs quality sampling."""
    key = f"{phase}_sample_rows"
    rows = node_params.get(key) or node_params.get("sample_rows")
    if rows is None:
        return df
    try:
        n = max(1, int(rows))
    except (TypeError, ValueError):
        return df
    return df.head(n)


def _run_profile_scan(node: PipelineNode, table: str, ctx: ToolContext) -> dict[str, Any]:
    state, err = _ensure_dataframe(node, table, ctx)
    if err:
        return err

    cfg = build_scan_config(
        table, node.params, redibis_config=ctx._redibis_config(), scan_types=["profile"]
    )
    scan = state.ensure_scan(cfg, redibis_config=ctx._redibis_config())
    _, tbl_name = scan_pipeline.split_table(table)
    state.profile = scan.profile(state.df, tbl_name=tbl_name)
    state.run_id = state.run_id or new_run_id()
    _persist_run_sample(state, cfg)
    return {
        "columns": len(state.profile.column_profiles),
        "run_id": state.run_id,
    }


def _run_quality_scan(node: PipelineNode, table: str, ctx: ToolContext) -> dict[str, Any]:
    state, err = _ensure_dataframe(node, table, ctx)
    if err:
        return err
    if state.profile is None:
        return {"skipped": True, "reason": "profile_scan must run before quality_scan"}

    from redibis.agents.run_defaults import merge_defaults_into_params

    params = merge_defaults_into_params(node.params)
    qdf = _subset_dataframe(state.df, params, phase="quality")

    cfg = build_scan_config(
        table, params, redibis_config=ctx._redibis_config(), scan_types=["quality"]
    )
    db_name, tbl_name = scan_pipeline.split_table(table)
    run_dir = Path(cfg.output_dir) / (state.run_id or new_run_id())
    run_dir.mkdir(parents=True, exist_ok=True)

    try:
        quality_contract, qa, quality_results, stats = run_quality_phase(
            qdf,
            cfg,
            state.profile,
            db_name=db_name,
            tbl_name=tbl_name,
            run_dir=run_dir,
        )
    except ImportError as exc:
        return {"skipped": True, "reason": f"quality engine unavailable: {exc}"}

    state.run_id = state.run_id or new_run_id()
    _persist_run_sample(state, cfg)
    state.extras["quality_contract"] = quality_contract
    state.extras["quality_stats"] = stats
    return {
        "run_id": state.run_id,
        "expectations": stats.get("total", 0),
        "passed": stats.get("passed", 0),
    }


def _run_pii_scan(node: PipelineNode, table: str, ctx: ToolContext) -> dict[str, Any]:
    state, err = _ensure_dataframe(node, table, ctx)
    if err:
        return err

    from redibis.agents.run_defaults import merge_defaults_into_params

    params = merge_defaults_into_params(node.params)
    pdf = _subset_dataframe(state.df, params, phase="pii")

    cfg = build_scan_config(
        table, params, redibis_config=ctx._redibis_config(), scan_types=["pii"]
    )
    scan = state.ensure_scan(cfg, redibis_config=ctx._redibis_config())
    try:
        detections = scan.detect_pii(pdf)
    except (ImportError, NotImplementedError) as exc:
        return {"skipped": True, "reason": f"PII engines unavailable: {exc}"}

    db_name, tbl_name = scan_pipeline.split_table(table)
    from redibis.contracts.type_inference import dtype_map_from_dataframe
    col_dtypes = dtype_map_from_dataframe(pdf)

    state.run_id = state.run_id or new_run_id()
    pii_partial = build_pii_contract(
        detections,
        db_name=db_name,
        tbl_name=tbl_name,
        config=cfg,
        run_id=state.run_id,
        col_dtypes=col_dtypes,
    )
    state.extras["pii_partial"] = pii_partial
    state.extras["pii_detections"] = detections
    _persist_run_sample(state, cfg)
    detected = sum(1 for d in detections if d.detected)
    return {
        "run_id": state.run_id,
        "columns_scanned": len(detections),
        "columns_detected": detected,
    }


def _run_contract(node: PipelineNode, table: str, ctx: ToolContext) -> dict[str, Any]:
    """Composite contract node — PII / quality / classify per config flags."""
    params = node.params
    results: dict[str, Any] = {"sub_steps": []}

    if params.get("quality", True):
        q_node = PipelineNode(kind="quality_scan", label="Quality", params=params)
        q_out = _run_quality_scan(q_node, table, ctx)
        results["sub_steps"].append({"kind": "quality", **q_out})
        if q_out.get("skipped") or q_out.get("failed"):
            return {**q_out, "sub_steps": results["sub_steps"]}

    if params.get("pii", True):
        p_node = PipelineNode(
            kind="pii_scan",
            label="PII",
            params={"equation_mode": params.get("equation_mode", "balanced")},
        )
        p_out = _run_pii_scan(p_node, table, ctx)
        results["sub_steps"].append({"kind": "pii", **p_out})
        if p_out.get("skipped") or p_out.get("failed"):
            return {**p_out, "sub_steps": results["sub_steps"]}

    if params.get("classify", True) and ctx.contract_store is not None:
        c_node = PipelineNode(
            kind="classify",
            label="Classify",
            params={
                "policy_pack": params.get("policy_pack", "telecom"),
                "jurisdiction": params.get("jurisdiction", ""),
                "use_memory": params.get("use_memory", False),
            },
        )
        c_out = _run_classify(c_node, table, ctx)
        results["sub_steps"].append({"kind": "classify", **c_out})

    if params.get("enrich"):
        write_err = _ensure_active_contract(node, table, ctx)
        if write_err:
            return {
                "skipped": True,
                "reason": write_err,
                "sub_steps": results["sub_steps"],
            }
        e_node = PipelineNode(kind="enrich", label="Enrich", params=params)
        e_out = _run_enrich(e_node, table, ctx)
        results["sub_steps"].append({"kind": "enrich", **e_out})
        if e_out.get("skipped") or e_out.get("failed"):
            return {**e_out, "sub_steps": results["sub_steps"]}

    state = get_table_state(ctx.run_states, table)
    return {
        "run_id": state.run_id,
        "sub_steps": results["sub_steps"],
        "kinds": [s["kind"] for s in results["sub_steps"]],
    }


def _contract_draft_for_table(table: str, ctx: ToolContext) -> Optional[dict[str, Any]]:
    """In-memory contract draft from scan partials (mirrors ``Scan.run`` classification)."""
    from redibis.scan.classification_phase import contract_for_classification

    state = get_table_state(ctx.run_states, table)
    draft = contract_for_classification(
        pii_partial=state.extras.get("pii_partial"),
        quality_partial=state.extras.get("quality_contract"),
    )
    if draft is not None:
        return draft
    if ctx.contract_store is not None:
        return ctx.contract_store.get_active(table)
    return None


def _ensure_active_contract(node: PipelineNode, table: str, ctx: ToolContext) -> Optional[str]:
    """Write scan partials + automerge when enrich/publish need an active contract."""
    if ctx.contract_store is None:
        return "no contract store"
    if ctx.contract_store.get_active(table) is not None:
        return None
    automerge = str(node.params.get("automerge") or "both")
    write_node = PipelineNode(
        kind="contract_write",
        label="Contract write",
        params={"automerge": automerge, "auto_approve": True},
    )
    out = _run_contract_write(write_node, table, ctx)
    if out.get("skipped"):
        return str(out.get("reason") or "contract write skipped")
    return None


def _run_classify(node: PipelineNode, table: str, ctx: ToolContext) -> dict[str, Any]:
    contract = _contract_draft_for_table(table, ctx)
    if contract is None:
        return {"skipped": True, "reason": "no contract draft — run profile/quality/pii first"}

    from redibis.classification import ClassificationService, JurisdictionContext, get_builtin_pack
    from redibis.services import pipeline

    pack_name = str(node.params.get("policy_pack") or "telecom")
    svc = ctx.classification_service or ClassificationService(get_builtin_pack(pack_name))
    redibis_cfg = ctx._redibis_config()
    from dataclasses import replace

    cls_cfg = replace(redibis_cfg.classification, enabled=True)
    if pack_name:
        cls_cfg = replace(cls_cfg, policy_pack=pack_name)
    redibis_cfg = replace(redibis_cfg, classification=cls_cfg)
    jurisdiction = str(node.params.get("jurisdiction") or "")
    if jurisdiction:
        svc.set_jurisdiction(JurisdictionContext(table=table, jurisdiction=jurisdiction))

    rows = pipeline.run_classification(
        contract,
        table,
        config=redibis_cfg,
        classification_service=svc,
    )
    state = get_table_state(ctx.run_states, table)
    state.extras["classification"] = {row["column"]: row["tags"] for row in rows}
    return {
        "columns": len(rows),
        "tags": {row["column"]: row["tags"] for row in rows},
        "escalations": [e for row in rows for e in row["escalations"]],
    }


def _run_enrich(node: PipelineNode, table: str, ctx: ToolContext) -> dict[str, Any]:
    if ctx.contract_store is None:
        return {"skipped": True, "reason": "no contract store"}
    if ctx.dry_run:
        return {"skipped": True, "reason": "enrich skipped in dry_run (LLM call)"}

    provider_name = str(node.params.get("provider") or "")
    model_name = str(node.params.get("model") or "")

    try:
        from redibis.enrich.capability_routing import (
            RoutingError,
            RoutingSnapshot,
            get_provider_for_role,
        )
        from redibis.enrich.service import enrichment_service_for_store
        from redibis.config import load_global_settings_optional
    except ImportError:
        return {"skipped": True, "reason": "enrich extra not installed (litellm)"}

    redibis_cfg = ctx._redibis_config()
    snapshot = None
    routing_raw = (getattr(ctx, "capability_routing", None) or {})
    if isinstance(routing_raw, dict) and routing_raw.get("roles"):
        try:
            snapshot = RoutingSnapshot.from_dict(routing_raw)
        except Exception:
            snapshot = None

    try:
        provider, _binding = get_provider_for_role(
            "contract.enrichment",
            snapshot=snapshot,
            gs=None if snapshot is not None else load_global_settings_optional(),
            agents_cfg=getattr(redibis_cfg, "agents", None),
            override_provider=provider_name,
            override_model=model_name,
        )
    except RoutingError as exc:
        if not provider_name:
            return {"skipped": True, "reason": str(exc)}
        try:
            from redibis.enrich.providers import get_provider

            provider = get_provider(provider_name, model=model_name)
        except ValueError as exc2:
            return {"skipped": True, "reason": str(exc2)}
    except ValueError as exc:
        return {"skipped": True, "reason": str(exc)}

    svc = enrichment_service_for_store(ctx.contract_store)

    residency = str(node.params.get("residency") or "")
    use_memory = bool(node.params.get("use_memory", True))
    from redibis.config import MemoryConfig

    memory_config = (
        redibis_cfg.memory
        if use_memory and redibis_cfg.memory.enabled
        else MemoryConfig(enabled=False)
    )

    try:
        from datetime import datetime, timezone

        from redibis.store.run_output_writer import RunOutputWriter

        state = get_table_state(ctx.run_states, table)
        run_id = state.run_id or new_run_id()
        run_writer = None
        if ctx.contract_store is not None:
            runs_bucket = getattr(ctx.config.storage, "runs_bucket", "pii-reports")
            run_writer = RunOutputWriter(
                backend=ctx.contract_store.backend,
                bucket=runs_bucket,
                workflow="enrich",
                table=table,
                run_id=run_id,
            )

        target = str(node.params.get("target") or "local").lower()
        sample_policy = "masked" if target in ("external", "cloud") else "raw"
        extra = str(node.params.get("extra_instructions") or node.params.get("repair_critique") or "")

        result = svc.enrich(
            table,
            provider,
            enriched_by="agent_pipeline",
            memory_config=memory_config,
            redibis_config=redibis_cfg,
            residency=residency,
            run_writer=run_writer,
            run_id=run_id,
            similar_context_enabled=use_memory,
            extra_instructions=extra or None,
            sample_policy=sample_policy,
            write_active=bool(node.params.get("write_active", True)),
        )
    except (PermissionError, KeyError) as exc:
        return {"skipped": True, "reason": str(exc)}

    meta = result.enrichment_meta or {}
    deferred = bool(result.deferred_context) and not result.auto_written
    if deferred:
        state.extras["pending_enrich"] = result

    from redibis.agents.enrich_validation import enrich_output_validation_fields

    out = {
        "table": result.table,
        "warnings": len(result.warnings),
        "provider": provider_name,
        "model": str(meta.get("model") or getattr(provider, "model", "") or ""),
        "model_purpose": "contract_enrichment",
        "auto_written": result.auto_written,
        "deferred_write": deferred,
        "version_after": result.version_after,
        "enrichment_status": result.enrichment_status,
        "run_id": run_id,
        "contract_table": table,
        **enrich_output_validation_fields(
            result_valid=result.valid,
            result_errors=list(result.errors),
            enrichment_meta=meta,
            candidate=result.candidate,
        ),
    }
    if result.run_artifacts:
        out["artifacts"] = dict(result.run_artifacts)
    rai_report = (result.enrichment_meta or {}).get("rai")
    if rai_report:
        out["rai"] = rai_report
        if rai_report.get("advisory_count"):
            out["rai_advisories"] = rai_report.get("advisories")
    return out


def _run_contract_synthesis(node: PipelineNode, table: str, ctx: ToolContext) -> dict[str, Any]:
    """Portable ODCS v3.1 synthesis — never upserts active contracts."""
    if ctx.dry_run:
        return {"skipped": True, "reason": "contract_synthesis skipped in dry_run"}
    contract_file = str(node.params.get("contract_file") or "").strip()
    if not contract_file:
        # Fall back to active contract export to a temp candidate file via store read.
        if ctx.contract_store is None:
            return {"skipped": True, "reason": "contract_file required (or contract store)"}
        try:
            active = ctx.contract_store.get_active(table)
        except Exception as exc:
            return {"skipped": True, "reason": f"no base contract: {exc}"}
        if not active:
            return {"skipped": True, "reason": "no active contract for table"}
        base_contract = active
    else:
        base_contract = contract_file

    reqs = [p.strip() for p in str(node.params.get("requirements") or "").split(",") if p.strip()]
    sources = [p.strip() for p in str(node.params.get("sources") or "").split(",") if p.strip()]
    output_dir = str(node.params.get("output_dir") or "./synthesis_out")
    mode = str(node.params.get("analysis_mode") or "deterministic")
    provider = None
    provider_name = str(node.params.get("provider") or "")
    if mode == "assisted" and provider_name:
        try:
            from redibis.enrich.providers import get_provider
            provider = get_provider(provider_name)
        except Exception as exc:
            return {"skipped": True, "reason": str(exc)}

    from redibis.synthesis import ContractSynthesisRunner

    runner = ContractSynthesisRunner(
        analysis_mode=mode,
        provider=provider,
        redibis_config=ctx._redibis_config(),
    )
    try:
        result = runner.run(
            base_contract=base_contract,
            requirement_paths=reqs,
            source_paths=sources,
            output_dir=output_dir,
        )
    except Exception as exc:
        return {"skipped": True, "reason": str(exc)}

    state = get_table_state(ctx.run_states, table)
    state.extras["synthesis"] = {
        "valid": result.valid,
        "artifacts": result.artifacts,
        "apiVersion": (result.candidate or {}).get("apiVersion"),
    }
    return {
        "table": table,
        "valid": result.valid,
        "errors": result.errors,
        "warnings": result.warnings,
        "artifacts": result.artifacts,
        "traceability": (result.traceability or {}).get("coverage"),
        "writes_active_contract": False,
        "apiVersion": (result.candidate or {}).get("apiVersion"),
    }


def _run_approval_gate(node: PipelineNode, table: str, ctx: ToolContext) -> dict[str, Any]:
    state = get_table_state(ctx.run_states, table)
    if node.params.get("auto_approve"):
        state.approval_granted = True
        return {"approved": True, "role": node.params.get("role", "Steward"), "auto": True}
    return {
        "reason": "approval gate — awaits human",
        "role": node.params.get("role", "Steward"),
    }


def _run_contract_write(node: PipelineNode, table: str, ctx: ToolContext) -> dict[str, Any]:
    state = get_table_state(ctx.run_states, table)
    auto = bool(node.params.get("auto_approve"))
    if not state.approval_granted and not auto:
        return {
            "skipped": True,
            "reason": "contract write blocked — approval gate not satisfied",
        }
    if ctx.sub_store is None:
        return {"skipped": True, "reason": "no subcontract store"}

    run_id = state.run_id or new_run_id()
    written: list[str] = []
    merger = ctx.run_merger
    if merger is None and ctx.contract_store is not None:
        merger = RunMerger(ctx.contract_store, ctx.sub_store)

    automerge = str(node.params.get("automerge") or "none")
    scan_cfg = build_scan_config(
        table,
        {"automerge": automerge},
        redibis_config=ctx._redibis_config(),
    )

    contract_uuid = None
    if ctx.contract_store is not None:
        active = ctx.contract_store.get_active(table)
        if active:
            contract_uuid = active.get("contract_uuid")

    if state.extras.get("pii_partial"):
        ScanContractWriter.write_kind(
            "pii",
            table=table,
            run_id=run_id,
            payload=state.extras["pii_partial"],
            sub_store=ctx.sub_store,
            contract_uuid=contract_uuid,
        )
        written.append("pii")
        if scan_cfg.automerges("pii") and merger is not None:
            merger.merge_run("pii", table, run_id, validate=scan_cfg.validate_contracts)

    if state.extras.get("quality_contract"):
        ScanContractWriter.write_kind(
            "quality",
            table=table,
            run_id=run_id,
            payload=state.extras["quality_contract"],
            sub_store=ctx.sub_store,
            contract_uuid=contract_uuid,
            summary_stats=state.extras.get("quality_stats") or {},
        )
        written.append("quality")
        if scan_cfg.automerges("quality") and merger is not None:
            merger.merge_run("quality", table, run_id, validate=scan_cfg.validate_contracts)

    if not written:
        return {"skipped": True, "reason": "no scan partials to write — run profile/quality/pii first"}

    return {"run_id": run_id, "written": written, "automerge": automerge}


def _run_deep_scan(node: PipelineNode, table: str, ctx: ToolContext) -> dict[str, Any]:
    state, err = _ensure_dataframe(node, table, ctx)
    if err:
        return err

    from redibis.agents.deep_scan import run_deep_scan
    from redibis.telemetry.init import set_run_dir

    cfg = ctx._redibis_config()
    state.run_id = state.run_id or new_run_id()
    run_dir = Path(getattr(build_scan_config(table, node.params, redibis_config=cfg), "output_dir", "./agent_runs"))
    run_dir = run_dir / state.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    set_run_dir(run_dir)

    producers_raw = (node.params.get("producers") or "").strip()
    producers = [p.strip() for p in producers_raw.split(",") if p.strip()] or None
    build_bundle = bool(node.params.get("build_bundle", True))

    if not ctx.execute:
        return {"skipped": True, "reason": "execute disabled"}

    result = run_deep_scan(
        table,
        state.run_id,
        state.df,
        producers=producers,
        run_dir=run_dir,
        config=cfg,
        build_bundle=build_bundle,
    )
    return {
        "run_id": state.run_id,
        "evidence_count": len(result.get("evidence") or []),
        "errors": result.get("errors") or [],
        "manifest": result.get("manifest"),
        "bundle": result.get("bundle"),
    }


def _run_pii_tune(node: PipelineNode, table: str, ctx: ToolContext) -> dict[str, Any]:
    """Run PII scan and build tuning bundle — proposals only, never auto-applied."""
    state, err = _ensure_dataframe(node, table, ctx)
    if err:
        return err
    if not ctx.execute:
        return {"skipped": True, "reason": "execute disabled"}

    from redibis.agents.run_defaults import merge_defaults_into_params
    from redibis.pii.tuning import build_pii_tuning_bundle
    from redibis.telemetry.init import set_run_dir

    params = merge_defaults_into_params(node.params)
    cfg = build_scan_config(table, params, redibis_config=ctx._redibis_config(), scan_types=["pii"])
    scan = state.ensure_scan(cfg, redibis_config=ctx._redibis_config())
    pdf = _subset_dataframe(state.df, params, phase="pii")

    try:
        detections = scan.detect_pii(pdf)
    except (ImportError, NotImplementedError) as exc:
        return {"skipped": True, "reason": f"PII engines unavailable: {exc}"}

    state.run_id = state.run_id or new_run_id()
    run_dir = Path(cfg.output_dir) / state.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    set_run_dir(run_dir)

    bundle_path = None
    if bool(params.get("build_bundle", True)):
        bundle_path = build_pii_tuning_bundle(
            detections,
            run_dir,
            table=table,
        )

    state.extras["pii_detections"] = detections
    state.extras["pii_tune_bundle"] = str(bundle_path) if bundle_path else None
    return {
        "run_id": state.run_id,
        "columns_scanned": len(detections),
        "bundle": str(bundle_path) if bundle_path else None,
        "proposal": {"add": {}, "remove": [], "ner_label_groups": []},
        "status": "awaiting_approval",
    }


def _run_catalog_push(node: PipelineNode, table: str, ctx: ToolContext) -> dict[str, Any]:
    if ctx.contract_store is None or ctx.catalog_service is None:
        return {"skipped": True, "reason": "catalog or contract store unavailable"}
    dry = ctx.dry_run or bool(node.params.get("dry_run"))
    try:
        result = ctx.catalog_service.push(table, dry_run=dry)
        return {
            "dry_run": dry,
            "backend": result.backend,
            "entity_fqn": result.entity_fqn,
        }
    except Exception as exc:
        return {"skipped": True, "reason": str(exc)}
