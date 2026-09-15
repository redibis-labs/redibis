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

    p_fc = esub.add_parser(
        "from-contract",
        help="convert an active PII contract into an evaluation dataset JSON",
        description=(
            "The contract becomes the EXPECTED values. Score the result with "
            "`redibis eval run --target engine` — scoring --target contract "
            "against a contract-derived dataset is circular and always 1.0."
        ),
    )
    p_fc.add_argument("table", nargs="?", default="",
                      help="schema.table whose active contract to convert")
    p_fc.add_argument("--contract-file",
                      help="read the contract from a YAML/JSON file instead of the store")
    p_fc.add_argument("-o", "--out", help="write dataset JSON (default: stdout)")
    p_fc.add_argument("--data", help="CSV/Parquet/XLSX file to draw sample records from")
    p_fc.add_argument("--samples", type=int, default=0, metavar="N",
                      help="attach N sample records per column (default 0 = none; "
                           "requires --data). 10 is the usual review size.")
    p_fc.add_argument("--sample-mode", choices=["shape", "raw"], default="shape",
                      help="shape = length+token masks with no characters or digits "
                           "(default, needs no consent); raw = real values, requires "
                           "recorded steward sampling consent for every column and "
                           "makes the file non-portable")
    p_fc.add_argument("--require-consent", action="store_true",
                      help="also gate shape masks on recorded steward sampling "
                           "consent (raw values are always gated)")
    p_fc.add_argument("--tags", default="",
                      help="comma-separated agreed tag vocabulary; only these tags "
                           "are exported. Omit to export every tag the contract carries.")
    p_fc.add_argument("--tags-file",
                      help="file of agreed tags, one per line (merged with --tags)")
    p_fc.add_argument("--pii-only", action="store_true",
                      help="export only columns the contract marks as PII")
    p_fc.add_argument("--json", action="store_true",
                      help="compact JSON to stdout (no summary line on stderr)")
    # Contract lookup goes through the shared webapp store accessors, which
    # read S3_* / REDIBIS_CONFIGS_DIR from the environment — same path
    # `eval run --target contract` already uses.
    p_fc.add_argument("--config", help="redibis.yaml")


def _load_contract_file(path: str):
    import yaml

    raw = Path(path).read_text(encoding="utf-8")
    if path.lower().endswith((".json",)):
        return json.loads(raw)
    return yaml.safe_load(raw)


def _agreed_tags(args) -> "list[str] | None":
    """Allowlist from --tags / --tags-file, or None when neither was given."""
    parts: list[str] = []
    given = False
    if getattr(args, "tags", ""):
        given = True
        parts.extend(t.strip() for t in args.tags.split(","))
    if getattr(args, "tags_file", None):
        given = True
        for line in Path(args.tags_file).read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                parts.append(line)
    if not given:
        return None
    return [t for t in parts if t]


def run_eval_from_contract(args) -> int:
    from redibis.evaluation import (
        TableEvalError,
        dataset_from_contract,
        sample_values_from_frame,
    )

    if not args.table and not args.contract_file:
        print("eval from-contract: give a table name or --contract-file", file=sys.stderr)
        return 2
    if args.samples and not args.data:
        print("eval from-contract: --samples needs --data", file=sys.stderr)
        return 2
    if args.sample_mode == "raw" and args.contract_file:
        print(
            "eval from-contract: --sample-mode raw needs a contract store to read "
            "sampling consent from; it cannot be used with --contract-file",
            file=sys.stderr,
        )
        return 2

    consent = None
    contract = None
    if args.contract_file:
        try:
            contract = _load_contract_file(args.contract_file)
        except (OSError, ValueError) as exc:
            print(f"eval from-contract: cannot read contract: {exc}", file=sys.stderr)
            return 2
    else:
        from redibis.webapp.store_accessors import get_contract_store

        store = get_contract_store()
        contract = store.get_active(args.table)
        if contract is None:
            print(
                f"eval from-contract: no active contract for {args.table!r}",
                file=sys.stderr,
            )
            return 2
        consent = getattr(store, "sampling_consent", None)

    sample_values: dict = {}
    dtypes: dict = {}
    if args.data:
        try:
            df = _read_frame(args.data)
        except Exception as exc:
            print(f"eval from-contract: cannot read {args.data}: {exc}", file=sys.stderr)
            return 2
        dtypes = {str(c): str(df[c].dtype) for c in df.columns}
        if args.samples:
            for col in df.columns:
                sample_values[str(col)] = sample_values_from_frame(
                    df, col, int(args.samples)
                )

    try:
        dataset = dataset_from_contract(
            contract,
            table_name=args.table,
            tags_allowlist=_agreed_tags(args),
            pii_only=bool(args.pii_only),
            sample_values=sample_values,
            sample_mode=args.sample_mode if args.samples else "none",
            sample_count=int(args.samples or 0),
            consent=consent,
            require_consent=bool(args.require_consent),
            dtypes=dtypes,
        )
    except TableEvalError as exc:
        print(f"eval from-contract: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"eval from-contract: {exc}", file=sys.stderr)
        return 2

    # Never emit a dataset `eval run` would refuse. The usual cause is a
    # column the contract marks as PII without an entity type.
    from redibis.evaluation import validate_table_dataset

    try:
        validate_table_dataset(dataset)
    except TableEvalError as exc:
        blanks = [
            c["name"] for c in dataset["columns"]
            if c["is_pii"] and not c["entity_type"]
        ]
        print(f"eval from-contract: produced an invalid dataset: {exc}", file=sys.stderr)
        if blanks:
            print(
                "eval from-contract: these columns are marked PII in the contract "
                "but carry no entity type — set one with "
                f"`redibis contract add-pii {args.table or '<table>'} --column <name> "
                "--entity-type <TYPE>`: " + ", ".join(blanks[:10])
                + (" …" if len(blanks) > 10 else ""),
                file=sys.stderr,
            )
        return 2

    rendered = json.dumps(dataset, indent=2, ensure_ascii=False) + "\n"
    try:
        if args.out:
            Path(args.out).write_text(rendered, encoding="utf-8")
        else:
            print(rendered, end="")
    except OSError as exc:
        print(f"eval from-contract: cannot write dataset: {exc}", file=sys.stderr)
        return 2

    if args.out and not args.json:
        pii = sum(1 for c in dataset["columns"] if c["is_pii"])
        tagged = sum(1 for c in dataset["columns"] if c["tags"])
        withheld = (dataset.get("notes") or {}).get("samples_withheld_columns") or []
        print(
            f"eval from-contract: {len(dataset['columns'])} column(s) "
            f"({pii} PII, {tagged} tagged) → {args.out}  "
            f"[residency={dataset['residency']}]",
            file=sys.stderr,
        )
        if withheld:
            print(
                f"eval from-contract: samples withheld for {len(withheld)} column(s) "
                f"without steward consent: {', '.join(withheld[:8])}"
                + (" …" if len(withheld) > 8 else ""),
                file=sys.stderr,
            )
        if dataset.get("warning"):
            print(f"eval from-contract: WARNING {dataset['warning']}", file=sys.stderr)
    return 0


def _read_frame(path: str):
    import pandas as pd

    low = path.lower()
    if low.endswith(".parquet"):
        return pd.read_parquet(path)
    if low.endswith((".xlsx", ".xls")):
        return pd.read_excel(path)
    if low.endswith(".tsv"):
        return pd.read_csv(path, sep="\t", dtype=str, keep_default_na=False)
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def run_eval(args) -> int:
    if args.eval_action == "from-contract":
        return run_eval_from_contract(args)

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
