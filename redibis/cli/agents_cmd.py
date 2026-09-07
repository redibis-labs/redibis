"""CLI for agentic pipeline board and batch orchestration."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

from redibis.agents import (
    BatchExecutor,
    LineageStore,
    PipelineSpec,
    ToolContext,
    compile_prompt_plan,
    diff_prompt_sections,
    list_nodes,
    merge_manual_edits,
    run_to_react_flow,
)
from redibis.agents.models import PipelineEdge, PipelineNode
from redibis.config import RedibisConfig
from redibis.services.catalog_service import CatalogService


def _tool_context(args, store, *, execute: bool = True) -> ToolContext:
    cfg = _load_config(args)
    from redibis.store.subcontract_store import SubcontractStore
    from redibis.store.run_merger import RunMerger
    from pathlib import Path

    backend = store.backend if store else None
    sub_store = SubcontractStore(backend) if backend else None
    merger = RunMerger(store, sub_store) if store and sub_store else None
    sample_dir = getattr(args, "sample_dir", None) or cfg.agents.sample_dir or None

    catalog = None
    if store:
        catalog = CatalogService.from_redibis_config(store, cfg)

    return ToolContext(
        contract_store=store,
        catalog_service=catalog,
        config=cfg,
        dry_run=getattr(args, "dry_run", False),
        execute=execute,
        sample_paths=_parse_sample_paths(getattr(args, "sample_paths", None)),
        sample_dir=Path(sample_dir) if sample_dir else None,
        sub_store=sub_store,
        run_merger=merger,
    )


def _parse_sample_paths(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    out: dict[str, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if "=" in part:
            table, path = part.split("=", 1)
            out[table.strip()] = path.strip()
    return out


def run_agents(args, store=None) -> int:
    if args.agents_action == "nodes":
        nodes = [n.to_dict() for n in list_nodes()]
        if getattr(args, "json", False):
            print(json.dumps({"nodes": nodes}, indent=2))
        else:
            for spec in list_nodes():
                print(f"{spec.kind:20} {spec.label:20} {spec.tool}")
        return 0

    if args.agents_action == "compile":
        spec = _load_spec(args)
        plan = compile_prompt_plan(spec)
        if getattr(args, "edits_file", None):
            edits = yaml.safe_load(Path(args.edits_file).read_text(encoding="utf-8")) or {}
            before = compile_prompt_plan(spec)
            plan = merge_manual_edits(plan, edits)
            if getattr(args, "show_diff", False):
                diff = diff_prompt_sections(before, plan)
                print(json.dumps(diff, indent=2))
                return 0

        if getattr(args, "json", False):
            print(json.dumps(plan.to_dict(), indent=2))
        elif getattr(args, "output", None):
            Path(args.output).write_text(plan.to_text(), encoding="utf-8")
            print(f"Wrote prompt-plan to {args.output}")
        else:
            print(plan.to_text())
        return 0

    if args.agents_action == "example":
        spec = _example_pipeline()
        plan = compile_prompt_plan(spec)
        if getattr(args, "json", False):
            print(json.dumps({"pipeline": spec.to_dict(), "plan": plan.to_dict()}, indent=2))
        else:
            print(plan.to_text())
        return 0

    lineage = _lineage_store(args)
    if args.agents_action == "runs":
        runs = lineage.list_runs(limit=getattr(args, "limit", 50))
        if getattr(args, "json", False):
            print(json.dumps({"runs": runs}, indent=2))
        else:
            for r in runs:
                summary = r.get("ledger_summary") or {}
                print(
                    f"{r.get('run_id')}  {r.get('status'):10}  "
                    f"{summary.get('done', 0)}/{summary.get('total', 0)} tasks  "
                    f"{r.get('name', '')}"
                )
        return 0

    if args.agents_action == "status":
        run = lineage.load(args.run_id)
        payload = run.to_dict()
        if getattr(args, "json", False):
            print(json.dumps(payload, indent=2))
        else:
            print(f"run_id: {run.run_id}")
            print(f"status: {run.status.value}")
            print(f"tables: {', '.join(run.tables)}")
            print(f"ledger: {run.ledger.reconcile()}")
        return 0

    if args.agents_action == "trace":
        run = lineage.load(args.run_id)
        trace = run_to_react_flow(run)
        if getattr(args, "json", False):
            print(json.dumps(trace, indent=2))
        else:
            print(f"run {run.run_id}: {len(trace['nodes'])} nodes, ledger={trace['ledger']}")
        return 0

    if args.agents_action == "cancel":
        ok = lineage.request_cancel(args.run_id, reason=getattr(args, "reason", "") or "CLI cancel")
        if not ok:
            print(f"could not cancel run {args.run_id!r}", file=sys.stderr)
            return 1
        print(f"cancel requested for {args.run_id}")
        return 0

    if args.agents_action == "run":
        if store is None:
            print("batch run requires contract store (not available for this command)", file=sys.stderr)
            return 2
        spec = _load_spec(args)
        lineage = _lineage_store(args)
        ctx = _tool_context(args, store)
        executor = BatchExecutor(lineage, tool_ctx=ctx)
        tables = _parse_tables_arg(getattr(args, "tables", None))
        run = executor.run(
            spec,
            tables=tables,
            database=getattr(args, "database", "") or "",
            dry_run=getattr(args, "dry_run", False),
        )
        if getattr(args, "json", False):
            print(json.dumps(run.to_dict(), indent=2))
        else:
            print(f"run {run.run_id} finished: {run.status.value}")
            print(f"ledger: {run.ledger.reconcile()}")
        return 0 if run.status.value in ("completed", "cancelled") else 1

    if args.agents_action == "execute":
        if store is None:
            print("execute requires contract store", file=sys.stderr)
            return 2
        table = args.table
        spec = _load_spec(args)
        lineage = _lineage_store(args)
        from redibis.agents.pipeline_executor import PipelineExecutor

        ctx = _tool_context(args, store)
        executor = PipelineExecutor(lineage, tool_ctx=ctx)
        run = executor.execute(
            spec,
            table,
            dry_run=getattr(args, "dry_run", False),
            sample_path=getattr(args, "sample", None),
        )
        if getattr(args, "json", False):
            print(json.dumps(run.to_dict(), indent=2))
        else:
            for step in run.steps:
                print(f"  {step.node_kind:16} {step.status.value:10} {step.table}")
        return 0 if run.status.value == "completed" else 1

    if args.agents_action == "docgen":
        spec = _load_spec(args)
        from redibis.agents.docgen import document_pipeline
        doc = document_pipeline(spec)
        if getattr(args, "json", False):
            print(json.dumps(doc, indent=2))
        elif getattr(args, "output", None):
            Path(args.output).write_text(doc["markdown"], encoding="utf-8")
            print(f"Wrote documentation to {args.output}")
        else:
            print(doc["markdown"])
        return 0

    if args.agents_action == "plugins":
        from redibis.agents.plugins import plugin_registry
        plugins = {k: v.to_dict() for k, v in plugin_registry().items()}
        if getattr(args, "json", False):
            print(json.dumps({"plugins": plugins}, indent=2))
        else:
            for kind in sorted(plugins):
                print(kind)
        return 0

    print(f"unknown agents action: {args.agents_action}", file=sys.stderr)
    return 2


def _load_config(args) -> RedibisConfig:
    import os
    path = getattr(args, "config", None) or os.environ.get("REDIBIS_CONFIG")
    if path:
        return RedibisConfig.from_yaml(path)
    return RedibisConfig.default()


def _lineage_store(args) -> LineageStore:
    cfg = _load_config(args)
    root = Path(getattr(args, "runs_dir", None) or cfg.agents.runs_dir)
    return LineageStore(root)


def _parse_tables_arg(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    return [t.strip() for t in raw.split(",") if t.strip()]


def _load_spec(args) -> PipelineSpec:
    if getattr(args, "pipeline_file", None):
        raw = yaml.safe_load(Path(args.pipeline_file).read_text(encoding="utf-8")) or {}
        return PipelineSpec.from_dict(raw)
    return _example_pipeline()


def _example_pipeline() -> PipelineSpec:
    source = PipelineNode(kind="source_table", label="Customers", params={
        "table": "telecom.customers", "engine": "hive",
    })
    profile = PipelineNode(kind="profile_scan", label="Profile")
    pii = PipelineNode(kind="pii_scan", label="PII", params={"equation_mode": "balanced"})
    classify = PipelineNode(kind="classify", label="Classify", params={
        "policy_pack": "telecom", "jurisdiction": "EU",
    })
    enrich = PipelineNode(kind="enrich", label="Enrich")
    gate = PipelineNode(kind="approval_gate", label="Steward gate", params={"role": "Steward"})
    write = PipelineNode(kind="contract_write", label="Write contract")

    nodes = [source, profile, pii, classify, enrich, gate, write]
    edges = [
        PipelineEdge(source=source.id, target=profile.id),
        PipelineEdge(source=profile.id, target=pii.id),
        PipelineEdge(source=pii.id, target=classify.id),
        PipelineEdge(source=classify.id, target=enrich.id),
        PipelineEdge(source=enrich.id, target=gate.id),
        PipelineEdge(source=gate.id, target=write.id),
    ]
    return PipelineSpec(
        name="telecom-customers-governance",
        goal="Profile, scan, classify, enrich, and publish telecom.customers",
        source={"table": "telecom.customers", "engine": "hive"},
        nodes=nodes,
        edges=edges,
    )
