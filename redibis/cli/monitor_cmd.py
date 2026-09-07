"""redibis quality-monitor — continuous validate-only *quality* monitoring CLI.

The canonical command name is ``quality-monitor``: this service validates
quality expectations only. It does not monitor PII, business definitions, or
general contract lifecycle, and the name must not imply that it does.
``redibis monitor ...`` stays as a deprecated alias for one cycle.
"""

from __future__ import annotations

import fnmatch
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from redibis.config import RedibisConfig

if TYPE_CHECKING:  # pragma: no cover - typing only
    from redibis.services.continuous_quality import ContinuousQualityService

# Everything below `register_monitor_commands` is import-time cold on purpose:
# building the argument parser must not drag Great Expectations (via
# continuous_quality → contract_validate) into a `redibis --help` process.

_GLOB_CHARS = frozenset("*?[")

QUALITY_MONITOR_CMD = "quality-monitor"
LEGACY_MONITOR_CMD = "monitor"
MONITOR_COMMANDS = (QUALITY_MONITOR_CMD, LEGACY_MONITOR_CMD)


def register_monitor_commands(sub, common_fn=None) -> list:
    """Register ``quality-monitor`` plus the deprecated ``monitor`` alias."""
    parsers = _register_one(
        sub,
        QUALITY_MONITOR_CMD,
        help_text="continuous validate-only quality monitoring",
        common_fn=common_fn,
    )
    parsers += _register_one(
        sub,
        LEGACY_MONITOR_CMD,
        help_text="deprecated alias for 'quality-monitor'",
        common_fn=common_fn,
    )
    return parsers


def _register_one(sub, name: str, *, help_text: str, common_fn=None) -> list:
    mon = sub.add_parser(name, help=help_text)
    mon_sub = mon.add_subparsers(dest="monitor_action", required=True)
    parsers = [mon]

    p_run = mon_sub.add_parser("run", help="validate one table against its active contract")
    p_run.add_argument("table", help="schema.table")
    _monitor_common(p_run)
    parsers.append(p_run)

    p_batch = mon_sub.add_parser("batch", help="validate multiple tables")
    p_batch.add_argument(
        "--tables",
        help="comma-separated schema.table list; entries with * ? [ are matched "
             "as glob patterns against tables with an active contract (e.g. 'golden.*')",
    )
    p_batch.add_argument("--database", help="filter contracts by database/schema prefix")
    p_batch.add_argument("--all-contracts", action="store_true", help="all tables with active contracts")
    p_batch.add_argument("--tables-file", help="one table per line")
    _monitor_common(p_batch)
    parsers.append(p_batch)

    p_export = mon_sub.add_parser("export", help="generate per-table quality monitoring package")
    p_export.add_argument("table", help="schema.table")
    p_export.add_argument("-o", "--output", required=True, help="output directory")
    p_export.add_argument("--schedule", default="0 6 * * *", help="cron for generated DAG")
    p_export.add_argument("--config", help="redibis.yaml path")
    p_export.add_argument(
        "--python-file",
        help="build the package from a generated quality program you edited in "
             "Jupyter/an IDE. Parsed with AST/literals only — never imported or "
             "executed. Without it, the effective active contract is used.",
    )
    p_export.add_argument(
        "--engine", choices=("spark", "pandas"), default="spark",
        help="engine for the generated program (default: spark — validates a "
             "whole partition in the cluster; pandas is local dev only)",
    )
    parsers.append(p_export)

    p_airflow = mon_sub.add_parser("airflow", help="generate Airflow DAG files")
    p_airflow_sub = p_airflow.add_subparsers(dest="airflow_action", required=True)
    parsers.append(p_airflow)
    p_gen = p_airflow_sub.add_parser("generate", help="write DAG files for tables")
    p_gen.add_argument(
        "--tables",
        help="comma-separated schema.table list; entries with * ? [ are matched "
             "as glob patterns against tables with an active contract",
    )
    p_gen.add_argument("--tables-file", help="one table per line")
    p_gen.add_argument("--database", help="filter by database prefix")
    p_gen.add_argument("--all-contracts", action="store_true")
    p_gen.add_argument("-o", "--output-dir", required=True, help="DAG output directory")
    p_gen.add_argument("--schedule", default="0 6 * * *")
    p_gen.add_argument("--no-event-dags", action="store_true")
    p_gen.add_argument("--config", help="redibis.yaml path")
    parsers.append(p_gen)

    if common_fn is not None:
        # `export` reads the active contract, so it needs the storage flags too.
        # `airflow generate` already owns `-o/--output-dir`; it relies on
        # `_contract_store`'s defaults instead of re-declaring those flags.
        for p in (p_run, p_batch, p_export):
            common_fn(p)
    return parsers


def _monitor_common(p) -> None:
    p.add_argument("--config", help="redibis.yaml; or REDIBIS_CONFIG env")
    p.add_argument(
        "--sample",
        help="path to CSV/Parquet sample file. For 'batch', may contain "
             "{table} / {table_safe} placeholders expanded per table "
             "(e.g. /data/{table_safe}.csv)",
    )
    p.add_argument("--rows", type=int, default=5000)
    p.add_argument("--json", action="store_true", help="machine-readable JSON summary")
    p.add_argument("--no-schema-drift", action="store_true")
    p.add_argument("--no-ge-docs", action="store_true")
    p.add_argument(
        "--no-publish", action="store_true",
        help="skip all configured result sinks (quality.publish.sinks / catalog.push.quality)",
    )
    p.add_argument("--rules-file", help="override contract rules with YAML QualityRuleSet")


def run_monitor(args, store, backend, *, runs_bucket: str = "pii-reports") -> int:
    if getattr(args, "cmd", None) == LEGACY_MONITOR_CMD:
        # stderr only: JSON stdout and exit codes stay byte-identical.
        print(
            "redibis monitor is deprecated; use 'redibis quality-monitor' "
            "(this command validates quality expectations only).",
            file=sys.stderr,
        )
    from redibis.services.continuous_quality import ContinuousQualityService

    cfg = _load_config(args)
    svc = ContinuousQualityService(store=store, backend=backend, runs_bucket=runs_bucket, config=cfg)

    action = args.monitor_action
    if action == "run":
        return _run_single(args, svc, store)
    if action == "batch":
        return _run_batch(args, svc)
    if action == "export":
        return _run_export(args, store)
    if action == "airflow":
        return _run_airflow_generate(args, svc)
    print(f"Unknown monitor action: {action}", file=sys.stderr)
    return 2


def _load_config(args) -> RedibisConfig:
    path = getattr(args, "config", None)
    if path:
        return RedibisConfig.from_yaml(path)
    # RedibisConfig.load() reads REDIBIS_CONFIG and falls back to defaults.
    return RedibisConfig.load()


def _monitor_options(args):
    from redibis.services.continuous_quality import MonitorRunOptions

    return MonitorRunOptions(
        rows=getattr(args, "rows", 5000),
        sample_path=getattr(args, "sample", None),
        include_schema_drift=not getattr(args, "no_schema_drift", False),
        generate_docs=not getattr(args, "no_ge_docs", False),
        publish_openmetadata=not getattr(args, "no_publish", False),
        output_dir=getattr(args, "output_dir", None),
        rules_file=getattr(args, "rules_file", None),
    )


def _has_explicit_selector(args) -> bool:
    return bool(
        getattr(args, "tables", None)
        or getattr(args, "tables_file", None)
        or getattr(args, "database", None)
        or getattr(args, "all_contracts", False)
    )


def _resolve_tables(args, svc: ContinuousQualityService) -> list[str]:
    if getattr(args, "tables_file", None):
        text = Path(args.tables_file).read_text(encoding="utf-8")
        tables = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
        return tables
    tables_arg = getattr(args, "tables", None)
    if tables_arg:
        raw = [t.strip() for t in tables_arg.split(",") if t.strip()]
        literal = [t for t in raw if not (_GLOB_CHARS & set(t))]
        patterns = [t for t in raw if _GLOB_CHARS & set(t)]
        if not patterns:
            return literal
        contracted = sorted(svc.store.list_tables())
        matched = list(literal)
        seen = set(literal)
        for pattern in patterns:
            for table in contracted:
                if table not in seen and fnmatch.fnmatch(table, pattern):
                    matched.append(table)
                    seen.add(table)
        return matched
    return svc.resolve_tables(
        database=getattr(args, "database", None),
        all_contracts=getattr(args, "all_contracts", False),
    )


def _run_single(args, svc: ContinuousQualityService, store) -> int:
    opts = _monitor_options(args)
    result = svc.run_table(args.table, opts)
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, default=str))
    else:
        print(
            f"{result.table}: {result.status} "
            f"({result.rules_passed}/{result.rules_total} passed)"
        )
        if result.schema_drift and (result.schema_drift.added or result.schema_drift.dropped):
            print(f"  schema drift: +{result.schema_drift.added} -{result.schema_drift.dropped}")
    return 0 if result.status in ("success", "skipped") else 1


def _run_batch(args, svc: ContinuousQualityService) -> int:
    if not _has_explicit_selector(args):
        print(
            "monitor batch requires --tables, --tables-file, --database, or "
            "--all-contracts (no implicit 'monitor everything')",
            file=sys.stderr,
        )
        return 2
    tables = _resolve_tables(args, svc)
    if not tables:
        print("No tables matched the given selector", file=sys.stderr)
        return 2
    opts = _monitor_options(args)
    batch = svc.run_batch(tables, opts)
    if args.json:
        print(json.dumps(batch.to_dict(), indent=2, default=str))
    else:
        print(
            f"batch {batch.run_id}: {batch.tables_passed} passed, "
            f"{batch.tables_failed} failed, {batch.tables_skipped} skipped "
            f"of {batch.tables_total}"
        )
    return 0 if batch.tables_failed == 0 else 1


def _run_export(args, store) -> int:
    engine = getattr(args, "engine", "spark") or "spark"
    python_file = getattr(args, "python_file", None)
    if python_file:
        return _run_export_from_python(args, python_file, engine)

    from redibis.services.continuous_quality import export_monitoring_bundle
    from redibis.store.quality_decisions import effective_contract_quality

    contract = store.get_active(args.table)
    if contract is None:
        print(f"No active contract for {args.table}", file=sys.stderr)
        return 2
    # Without --python-file, CLI export sources the effective active-contract
    # rule set (suppressed rules removed, approved manual rules included) —
    # never a web session's in-progress curation, which has no meaning here.
    decisions = store.quality_decisions.get(args.table)
    effective = effective_contract_quality(contract, decisions)
    path = export_monitoring_bundle(
        args.table,
        effective,
        schedule=getattr(args, "schedule", "0 6 * * *"),
        output_dir=args.output,
        engine=engine,
    )
    print(path)
    return 0


def _run_export_from_python(args, python_file: str, engine: str) -> int:
    """Rebuild a package from a generated program the operator edited.

    The file is parsed with AST/literals only — Redibis never imports or
    executes it. A parse failure, a non-literal rule definition, or a file with
    no extractable rules is an error: we do not quietly fall back to the
    contract, because that would ship rules the operator did not write.
    """
    import hashlib

    from redibis.quality.monitoring_package import build_monitoring_package
    from redibis.services.quality_code import rules_from_python_source

    path = Path(python_file)
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"Cannot read --python-file {python_file}: {e}", file=sys.stderr)
        return 2

    rules, errors = rules_from_python_source(source)
    hard_failure = any("Could not parse" in (e.get("message") or "") for e in errors)
    if hard_failure:
        for err in errors:
            print(f"{python_file}:{err.get('line', 0)}: {err.get('message')}", file=sys.stderr)
        return 2
    for err in errors:
        print(f"{python_file}:{err.get('line', 0)}: {err.get('message')}", file=sys.stderr)
    if not rules:
        print(
            f"No quality rules found in {python_file}. Expected the RULES = [...] "
            "block of a generated program, or add_gx_expectation(...) / expect_*(...) "
            "calls with literal arguments.",
            file=sys.stderr,
        )
        return 2

    out = build_monitoring_package(
        table=args.table,
        rules=rules,
        schedule=getattr(args, "schedule", "0 6 * * *"),
        output_dir=Path(args.output),
        rule_source="python_file",
        engine=engine,
        source_digest=hashlib.sha256(source.encode("utf-8")).hexdigest(),
    )
    print(out)
    return 0


def _run_airflow_generate(args, svc: ContinuousQualityService) -> int:
    from redibis.integrations.airflow.generate import generate_dags

    if not _has_explicit_selector(args):
        print(
            "monitor airflow generate requires --tables, --tables-file, --database, "
            "or --all-contracts (no implicit 'generate for everything')",
            file=sys.stderr,
        )
        return 2
    tables = _resolve_tables(args, svc)
    if not tables:
        print("No tables matched the given selector", file=sys.stderr)
        return 2
    paths = generate_dags(
        tables,
        args.output_dir,
        schedule=getattr(args, "schedule", "0 6 * * *"),
        include_event_dags=not getattr(args, "no_event_dags", False),
    )
    for p in paths:
        print(p)
    return 0
