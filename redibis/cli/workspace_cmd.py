"""CLI: ``redibis workspace …`` and helpers for ``scan-batch``.

Keep this module import-light so ``redibis --help`` does not pull scan / GE.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def register_workspace_commands(sub) -> None:
    p = sub.add_parser("workspace", help="named contract workspaces (local folder or MinIO prefix)")
    ssub = p.add_subparsers(dest="workspace_action", required=True)

    ssub.add_parser("list", help="known workspaces for this configs dir")

    p_add = ssub.add_parser("add", help="register a local folder or s3://bucket/prefix")
    p_add.add_argument("root", help="folder path or s3://bucket/prefix")
    p_add.add_argument("--name", default="", help="display name")
    p_add.add_argument("--read-only", action="store_true")

    p_rm = ssub.add_parser("remove", help="forget a slug (never deletes data)")
    p_rm.add_argument("slug")

    p_ix = ssub.add_parser("reindex", help="rebuild index.json from contracts")
    p_ix.add_argument("slug")

    p_ls = ssub.add_parser("contracts", help="paged listing from the index")
    p_ls.add_argument("slug")
    p_ls.add_argument("--q", default="")
    p_ls.add_argument("--status", default="")
    p_ls.add_argument("--review", default="")
    p_ls.add_argument("--limit", type=int, default=50)
    p_ls.add_argument("--offset", type=int, default=0)
    p_ls.add_argument("--json", action="store_true")

    p_en = ssub.add_parser("enrich", help="batch enrich tables in a workspace")
    p_en.add_argument("slug")
    p_en.add_argument("--tables", default="", help="comma-separated tables")
    p_en.add_argument("--q", default="")
    p_en.add_argument("--provider", default="demo")
    p_en.add_argument("--workers", type=int, default=1)
    p_en.add_argument("--resume", action="store_true")
    p_en.add_argument("--batch-id", default="")

    p_sy = ssub.add_parser("synthesize", help="batch synthesise tables in a workspace (portable artifacts; never writes active)")
    p_sy.add_argument("slug")
    p_sy.add_argument("--tables", default="")
    p_sy.add_argument("--q", default="")
    p_sy.add_argument(
        "--analysis-mode", default="deterministic",
        choices=["deterministic", "assisted"],
    )
    p_sy.add_argument("--provider", default="", help="required when --analysis-mode assisted")
    p_sy.add_argument("--resume", action="store_true")
    p_sy.add_argument("--batch-id", default="")

    p_ex = ssub.add_parser("export", help="zip artifacts for selected tables")
    p_ex.add_argument("slug")
    p_ex.add_argument("--tables", default="")
    p_ex.add_argument("--artifacts", default="contract,verdicts,evidence,llm_context,corpus,graph")
    p_ex.add_argument("-o", "--out", required=True)

    p_st = ssub.add_parser("batch-status", help="print a batch manifest")
    p_st.add_argument("slug")
    p_st.add_argument("batch_id")

    p_pr = ssub.add_parser("promote", help="copy a reviewed contract into another workspace")
    p_pr.add_argument("slug")
    p_pr.add_argument("table")
    p_pr.add_argument("--to", dest="target", default="default")
    p_pr.add_argument("--force", action="store_true", help="admin override when guarantee does not hold")


def register_scan_batch_command(sub, *, common, scan_flags) -> None:
    p = sub.add_parser(
        "scan-batch",
        help="scan a folder of tabular files into a workspace (resumable)",
    )
    common(p)
    p.add_argument("--input", required=True, help="folder of CSV/Parquet files")
    p.add_argument("--glob", default="*.csv")
    p.add_argument("--recursive", action="store_true")
    p.add_argument(
        "--workspace", required=True,
        help="local folder (created if absent) or s3://bucket/prefix",
    )
    p.add_argument(
        "--table-from", choices=["filename", "folder", "manifest.csv"],
        default="filename",
    )
    p.add_argument("--manifest", help="path,table CSV when --table-from manifest.csv")
    p.add_argument("--scan-mode", dest="mode", default="both",
                   help="both|pii|quality|all (same as scan --mode)")
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--resume", action="store_true")
    p.add_argument("--batch-id", default="")
    p.add_argument("--continue-on-error", action="store_true")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--steward-artifacts", action="store_true",
                   help="scan writes profile store + ledger (default scan path already does)")
    p.add_argument("--config", help="YAML config file (RedibisConfig)")
    p.add_argument("--equation", choices=["strict", "balanced", "lenient", "independent"],
                   default="independent")
    p.add_argument("--pii-engines", choices=["regex", "gliner", "ner", "llm", "both"],
                   default="both")


def run_workspace(args) -> int:
    from redibis.workspace.batch import BatchRunner, export_zip
    from redibis.workspace.model import WorkspaceDenied, WorkspaceError, WorkspaceNotFound
    from redibis.workspace.promote import promote_table
    from redibis.workspace.registry import get_registry
    from redibis.workspace.stores import stores_for

    action = args.workspace_action
    try:
        if action == "list":
            rows = get_registry().list()
            for ref in rows:
                print(f"{ref.slug:20} {ref.kind:6} {ref.root}")
            return 0
        if action == "add":
            ref = get_registry().add_from_root(args.root, name=args.name, read_only=args.read_only)
            print(ref.slug)
            return 0
        if action == "remove":
            get_registry().remove(args.slug)
            print(f"forgot {args.slug} (data kept)")
            return 0
        if action == "reindex":
            n = stores_for(args.slug).index.rebuild()
            print(f"reindexed {n} contracts")
            return 0
        if action == "contracts":
            stores = stores_for(args.slug)
            if not stores.index.rows():
                stores.index.rebuild()
            rows, total = stores.index.query(
                q=args.q, status=getattr(args, "status", ""),
                review=getattr(args, "review", ""),
                limit=args.limit, offset=args.offset,
            )
            payload = {"rows": [r.to_dict() for r in rows], "total": total,
                       "limit": args.limit, "offset": args.offset}
            if args.json:
                print(json.dumps(payload, indent=2))
            else:
                print(f"{total} contracts (showing {len(rows)})")
                for r in rows:
                    print(f"  {r.table:30} {r.contract_uuid[:8]:8} {r.review:12} {r.updated}")
            return 0
        if action in ("enrich", "synthesize"):
            stores = stores_for(args.slug)
            tables = [t.strip() for t in (args.tables or "").split(",") if t.strip()]
            if not tables:
                rows, _ = stores.index.query(q=args.q or "", limit=2000, offset=0)
                tables = [r.table for r in rows]
            runner = BatchRunner(stores)
            if action == "synthesize":
                options = {
                    "analysis_mode": getattr(args, "analysis_mode", "deterministic"),
                    "provider": getattr(args, "provider", "") or "",
                }
            else:
                options = {"provider": getattr(args, "provider", "demo")}
            bid = runner.submit(
                action, tables,
                options=options,
                resume=args.resume, batch_id=args.batch_id or None,
            )
            man = runner.run(bid)
            print(json.dumps({"id": man["id"], "counts": man.get("counts"), "status": man.get("status")}, indent=2))
            return 0 if not (man.get("counts") or {}).get("error") else 1
        if action == "export":
            stores = stores_for(args.slug)
            tables = [t.strip() for t in (args.tables or "").split(",") if t.strip()]
            if not tables:
                rows, _ = stores.index.query(limit=2000, offset=0)
                tables = [r.table for r in rows]
            arts = [a.strip() for a in args.artifacts.split(",") if a.strip()]
            blob = export_zip(stores, tables, arts)
            Path(args.out).write_bytes(blob)
            print(args.out)
            return 0
        if action == "batch-status":
            man = BatchRunner(stores_for(args.slug)).status(args.batch_id)
            print(json.dumps(man, indent=2, default=str))
            return 0
        if action == "promote":
            result = promote_table(
                stores_for(args.slug), args.table,
                target=args.target, force=args.force, actor="cli",
            )
            print(json.dumps(result, indent=2))
            return 0
    except (WorkspaceDenied, WorkspaceError, WorkspaceNotFound, KeyError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"unknown workspace action: {action}", file=sys.stderr)
    return 2


def run_scan_batch(args) -> int:
    from redibis.workspace.scan_batch import run_scan_batch as _run

    man = _run(
        input_dir=Path(args.input),
        workspace=args.workspace,
        glob_pat=args.glob,
        recursive=bool(args.recursive),
        table_from="manifest" if args.table_from == "manifest.csv" else args.table_from,
        manifest=Path(args.manifest) if getattr(args, "manifest", None) else None,
        workers=args.workers,
        resume=bool(args.resume),
        continue_on_error=bool(args.continue_on_error),
        limit=args.limit or None,
        options={
            "scan_mode": getattr(args, "mode", "both"),
            "config": getattr(args, "config", None),
            "steward_artifacts": bool(getattr(args, "steward_artifacts", False)),
            "equation": getattr(args, "equation", "independent"),
            "pii_engines": getattr(args, "pii_engines", "both"),
        },
        batch_id=getattr(args, "batch_id", "") or None,
    )
    print(json.dumps({
        "id": man.get("id"),
        "status": man.get("status"),
        "counts": man.get("counts"),
        "errors": man.get("errors") or [],
        "workspace": args.workspace,
    }, indent=2))
    if man.get("exit_nonzero"):
        return 1
    return 0
