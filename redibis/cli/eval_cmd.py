"""CLI: ``redibis eval validate|run`` — structured table-column evaluation."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def register_eval_commands(sub) -> None:
    p = sub.add_parser(
        "eval",
        help="validate or score a portable table-column evaluation dataset",
    )
    esub = p.add_subparsers(dest="eval_action", required=True)

    p_val = esub.add_parser("validate", help="validate dataset JSON")
    p_val.add_argument("--dataset", required=True)
    p_val.add_argument("--json", action="store_true")

    p_run = esub.add_parser("run", help="score dataset against engine, contract, or LLM proposals")
    p_run.add_argument("--dataset", required=True)
    p_run.add_argument("--data", help="CSV or Parquet file")
    p_run.add_argument("--table", default="", help="schema.table for contract lookup")
    p_run.add_argument(
        "--target",
        choices=["engine", "contract", "llm"],
        default="engine",
    )
    p_run.add_argument("--engines", default="both")
    p_run.add_argument("--equation", default="independent")
    p_run.add_argument("--semantic", action="store_true", help="optional LLM judge for definitions")
    p_run.add_argument("-o", "--out", help="write JSON report")
    p_run.add_argument("--html", help="write standalone HTML report")
    p_run.add_argument("--min-exact-f1", type=float)
    p_run.add_argument("--config", help="redibis.yaml")


def run_eval(args) -> int:
    from redibis.evaluation import (
        TableEvalError,
        from_column_evaluations_corpus,
        render_table_report_html,
        run_table_evaluation,
        validate_table_dataset,
    )

    path = Path(args.dataset)
    if not path.exists():
        print(f"eval: dataset not found: {path}", file=sys.stderr)
        return 2
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("column_evaluations") and raw.get("kind") != "redibis.table_column_eval_dataset":
            dataset = from_column_evaluations_corpus(raw)
        else:
            dataset = validate_table_dataset(raw)
    except (OSError, json.JSONDecodeError, TableEvalError) as exc:
        print(f"eval: invalid dataset: {exc}", file=sys.stderr)
        return 2

    if args.eval_action == "validate":
        if getattr(args, "json", False):
            print(json.dumps(dataset, indent=2, ensure_ascii=False))
        else:
            print(f"{len(dataset['columns'])} column(s) · table {dataset.get('table_name') or '—'}")
        return 0

    from redibis.config import RedibisConfig

    cfg = RedibisConfig.from_yaml(args.config) if getattr(args, "config", None) else RedibisConfig()
    contract = None
    if args.target == "contract":
        table = args.table or dataset.get("table_name") or ""
        if not table:
            print("eval: --table is required for --target contract", file=sys.stderr)
            return 2
        from redibis.webapp.store_accessors import get_contract_store

        contract = get_contract_store().get_active(table)
        if contract is None:
            print(f"eval: no active contract for {table!r}", file=sys.stderr)
            return 2
    if args.target != "contract" and not args.data:
        print("eval: --data is required for engine/llm targets", file=sys.stderr)
        return 2
    try:
        report = run_table_evaluation(
            dataset,
            data_path=args.data or "",
            contract=contract,
            target=args.target,
            engines=args.engines,
            equation=args.equation,
            redibis_config=cfg,
            semantic=bool(args.semantic),
        )
    except TableEvalError as exc:
        print(f"eval: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"eval: {exc}", file=sys.stderr)
        return 2

    rendered = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    try:
        if args.out:
            Path(args.out).write_text(rendered, encoding="utf-8")
        if args.html:
            Path(args.html).write_text(render_table_report_html(report), encoding="utf-8")
        if not args.out and not args.html:
            print(rendered, end="")
    except OSError as exc:
        print(f"eval: cannot write report: {exc}", file=sys.stderr)
        return 2
    exact_f1 = float(((report.get("exact") or {}).get("micro") or {}).get("f1") or 0.0)
    if args.min_exact_f1 is not None and exact_f1 < args.min_exact_f1:
        print(f"eval: exact micro F1 {exact_f1:.4f} is below {args.min_exact_f1:.4f}", file=sys.stderr)
        return 1
    return 0
