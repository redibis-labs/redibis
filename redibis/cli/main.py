from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

import yaml

from redibis.scan_mode import parse_scan_mode
from redibis.store.storage_backend import get_backend, S3Config, LocalBackend
from redibis.store.contract_store import ContractStore
from redibis.store.subcontract_store import SubcontractStore
from redibis.store.run_merger import RunMerger
from redibis.store.merger import merge_odcs_contracts


def _build_backend(args):
    """Default: local storage. Pass ``--use-s3`` to target S3/MinIO (credentials via env)."""
    if not getattr(args, "use_s3", False):
        root = Path(getattr(args, "output_dir", "./reports")) / "_dev_storage"
        return LocalBackend(str(root))

    cfg = S3Config.from_env()
    if getattr(args, "s3_endpoint", None):
        cfg.endpoint_url = args.s3_endpoint
    return get_backend(cfg)


def _contract_store(args, backend) -> ContractStore:
    """Build ContractStore; attach memory config when REDIBIS_CONFIG / --config is set."""
    import os
    from redibis.config import RedibisConfig

    # Not every subcommand declares the shared storage flags; fall back to the
    # same default those flags use rather than crashing before dispatch.
    kwargs: dict = {
        "backend": backend,
        "bucket": getattr(args, "s3_contracts_bucket", None) or "active-contracts",
    }
    path = getattr(args, "config", None) or os.environ.get("REDIBIS_CONFIG")
    if path:
        kwargs["memory_config"] = RedibisConfig.from_yaml(path).memory
    return ContractStore(**kwargs)


def _scan_tier_flags_set(args) -> bool:
    return any([
        getattr(args, "enable_metadata", False),
        getattr(args, "enable_pushdown", False),
        getattr(args, "enable_memory", False),
        getattr(args, "memory_domain", None),
        getattr(args, "memory_store", None),
        getattr(args, "source_engine", None),
    ])


def _run_report(args) -> int:
    """``redibis report …`` — delegates to commercial redibis-reports when installed."""
    from redibis.reporting import run_reporting_cli

    return run_reporting_cli(args)

def _run_business_import(args, store: ContractStore):
    table = args.table

    if args.file:
        with open(args.file) as f:
            partial = yaml.safe_load(f)
        if args.table_from_filename:
            stem = Path(args.file).stem
            parts = stem.split(".", 1)
            if len(parts) == 2:
                table = stem
    elif args.dir:
        dir_path = Path(args.dir)
        if not dir_path.is_dir():
            print(f"Directory not found: {args.dir}", file=sys.stderr)
            return 1
        results = []
        for yf in sorted(dir_path.glob("*.yaml")):
            with open(yf) as f:
                partial = yaml.safe_load(f)
            t = yf.stem if args.table_from_filename else table
            result = store.upsert(partial, table=t, workflow="business", run_id="manual")
            results.append(result)
            print(f"Upserted {t}: v{result.version_after} ({'new' if result.is_new else 'updated'})")
        return 0 if results else 1
    else:
        print("Provide --file or --dir", file=sys.stderr)
        return 1

    result = store.upsert(partial, table=table, workflow="business", run_id="manual")
    print(f"Upserted {table}: v{result.version_after} ({'new' if result.is_new else 'updated'})")
    return 0


def _build_subcontract_store(args, backend) -> SubcontractStore:
    return SubcontractStore(
        backend,
        pii_bucket=getattr(args, "s3_pii_runs_bucket", None) or "pii-contracts",
        quality_bucket=getattr(args, "s3_quality_runs_bucket", None) or "quality-contracts",
    )


def _run_runs(args, store: ContractStore, backend):
    """`redibis runs {list|merge|discard}` — select-and-merge from a run bucket."""
    sub_store = _build_subcontract_store(args, backend)
    merger = RunMerger(store, sub_store)
    action = args.runs_action

    if action == "list":
        runs = sub_store.list_run_summaries(args.kind, args.table)
        if not runs:
            print(f"No {args.kind} runs for {args.table}")
            return 0
        for r in runs:
            print(f"  {r['run_id']:40s}  {r['status']:10s}  "
                  f"{r.get('created_at','')}  {r.get('summary_stats', {})}")
        return 0

    if action == "merge":
        # CLI merge is an automated promotion of scan output → strip
        # value-bearing quality rules from PII columns (no raw PII in contract).
        if args.run:
            result = merger.merge_run(args.kind, args.table, args.run,
                                      validate=not args.no_validate,
                                      strip_pii_quality=True)
        else:
            result = merger.merge_latest(args.kind, args.table,
                                         validate=not args.no_validate,
                                         strip_pii_quality=True)
            if result is None:
                print(f"No mergeable {args.kind} runs for {args.table}", file=sys.stderr)
                return 1
        u = result.upsert
        print(f"Merged {args.kind} run {result.run_id} into {args.table}: "
              f"v{u.version_before or '∅'} → v{u.version_after} "
              f"({'new' if u.is_new else 'updated'})")
        return 0

    if action == "discard":
        sub = merger.discard_run(args.kind, args.table, args.run)
        if sub is None:
            print(f"No {args.kind} run {args.run!r} for {args.table}", file=sys.stderr)
            return 1
        print(f"Discarded {args.kind} run {args.run} for {args.table}")
        return 0

    return 1


def _run_rules(args, store: ContractStore):
    """`redibis rules export <table> --target ge|sodacl|dbt`."""
    from redibis.contracts.rules import regenerate, extract_rules
    active = store.get_active(args.table)
    if active is None:
        print(f"No active contract for {args.table}", file=sys.stderr)
        return 1
    if args.rules_action == "list":
        for r in extract_rules(active):
            print(f"  [{r.severity}] {r.type:14s} {r.column or '(table)':20s} {r.params}")
        return 0
    out = regenerate(active, args.target)
    if isinstance(out, dict):
        print(json.dumps(out, indent=2, default=str))
    else:
        print(out)
    return 0


def _run_contract_metadata(args, store: ContractStore) -> int:
    """`redibis contract metadata <table>`."""
    meta = store.get_metadata(args.table)
    if args.json:
        print(json.dumps(meta, indent=2, default=str))
    else:
        print(yaml.safe_dump(meta, default_flow_style=False, allow_unicode=True))
    return 0


def _run_contract_export_package(args, store: ContractStore) -> int:
    """`redibis contract export-package <table>` — spec + telemetry JSON bundle."""
    try:
        pkg = store.export_integration_package(args.table)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    print(json.dumps(pkg, indent=2, default=str))
    return 0


def _run_purge(args, store: ContractStore, backend):
    """`redibis contract purge <table> [--keep-runs]`."""
    result = store.purge(args.table)
    if not args.keep_runs:
        sub_store = _build_subcontract_store(args, backend)
        deleted = sub_store.delete_table_runs(args.table)
        result["deleted_runs"] = deleted
    print(f"Purged {args.table}: {result['deleted_count']} active/audit keys removed"
          + ("" if args.keep_runs else f"; run buckets cleared"))
    return 0


def _run_contract_view(args, store: ContractStore) -> int:
    """`redibis contract pii-view|quality-view|definitions-view <table>`."""
    try:
        if args.contract_action == "pii-view":
            data = store.get_pii_view(args.table)
        elif args.contract_action == "quality-view":
            data = store.get_quality_view(args.table)
        else:
            data = store.get_definitions_view(args.table)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(data, indent=2, default=str))
    else:
        print(yaml.safe_dump(data, default_flow_style=False, allow_unicode=True))
    return 0


def _run_quality_decision(args, store: ContractStore) -> int:
    """`redibis contract quality-suppress|quality-restore <table> --rule-id ID`."""
    if args.contract_action == "quality-suppress-all":
        try:
            res = store.suppress_all_quality_rules(
                args.table, decided_by="cli",
            )
            print(f"Suppressed all quality rules on {args.table} → v{res.version_after}")
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        return 0
    if not args.rule_id:
        print("error: --rule-id is required", file=sys.stderr)
        return 2
    try:
        if args.contract_action == "quality-suppress":
            res = store.suppress_quality_rule(
                args.table, args.rule_id, column=args.column, decided_by="cli",
            )
            print(f"Suppressed {args.rule_id} on {args.table} → v{res.version_after}")
        else:
            res = store.restore_quality_rule(args.table, args.rule_id)
            print(f"Restored {args.rule_id} on {args.table} → v{res.version_after}")
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


def _run_definitions_patch(args, store: ContractStore) -> int:
    """`redibis contract definitions-patch <table> --patch-file FILE`."""
    if not args.patch_file:
        print("error: --patch-file is required", file=sys.stderr)
        return 2
    with open(args.patch_file, encoding="utf-8") as fh:
        body = json.load(fh)
    try:
        res = store.patch_definitions(
            args.table,
            table_patch=body.get("table"),
            column_patches=body.get("columns"),
            decided_by="cli",
        )
        print(f"Patched definitions on {args.table} → v{res.version_after}")
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


def _run_pii_decision(args, store: ContractStore) -> int:
    """`redibis contract strip-pii|add-pii <table> --column C [--entity-type E]`."""
    if not args.column:
        print("error: --column is required", file=sys.stderr)
        return 2
    try:
        if args.contract_action == "strip-pii":
            res = store.set_pii_decision(args.table, args.column, "not_pii",
                                         decided_by="cli")
            print(f"Stripped PII from {args.table}.{args.column} → v{res.version_after}")
        else:  # add-pii
            if not args.entity_type:
                print("error: --entity-type is required for add-pii", file=sys.stderr)
                return 2
            from redibis.services.session_service import pii_row_to_fragment
            frag = pii_row_to_fragment({
                "column": args.column, "detected": True,
                "entity_type": args.entity_type, "confidence": args.confidence,
            })
            res = store.set_pii_decision(args.table, args.column, "pii",
                                         entity_type=args.entity_type, payload=frag,
                                         decided_by="cli")
            print(f"Marked {args.table}.{args.column} as PII ({args.entity_type}) "
                  f"→ v{res.version_after}")
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


def _table_from_file(path: str, table: Optional[str]) -> str:
    from pathlib import Path as _P
    if table:
        return table
    stem = _P(path).stem
    return stem if "." in stem else f"data.{stem}"


def _load_mask_df(path: str):
    import pandas as pd
    return pd.read_parquet(path) if path.endswith((".parquet", ".pq")) else pd.read_csv(path)


def _detect_for_mask(df) -> list[dict]:
    """Best-effort PII detection to drive auto-suggest; [] if engines missing."""
    try:
        from redibis.services import pipeline
        dets = pipeline.run_pii_detection(df, engines="both")
        return [{"column": d.column, "detected": d.detected,
                 "entity_type": d.entity_type} for d in dets]
    except Exception as e:
        print(f"  (PII detection unavailable: {e}; defaulting columns to passthrough)",
              file=sys.stderr)
        return []


def _run_data(args) -> int:
    """`redibis data preview <file> --rows N`."""
    df = _load_mask_df(args.file)
    cols = df.columns.tolist()
    print(f"{args.file}: {len(df)} rows × {len(cols)} cols")
    print(" | ".join(str(c) for c in cols))
    print("-" * 60)
    for _, row in df.head(args.rows).iterrows():
        print(" | ".join("" if pd_isna(v) else str(v) for v in row.tolist()))
    return 0


def pd_isna(v) -> bool:
    try:
        import pandas as pd
        return bool(pd.isna(v))
    except Exception:
        return v is None


def _mask_default_locale(args) -> str:
    import os
    from redibis.config import RedibisConfig
    from redibis.masking import transforms as T

    if getattr(args, "locale", None):
        return T.canonicalize_faker_locale(args.locale)
    path = getattr(args, "config", None) or os.environ.get("REDIBIS_CONFIG")
    if path:
        return T.canonicalize_faker_locale(RedibisConfig.from_yaml(path).masking.default_locale)
    return "default"


def _parse_column_faker_overrides(values: Optional[list[str]]) -> dict[str, str]:
    from redibis.masking import transforms as T

    out: dict[str, str] = {}
    for raw in values or []:
        if "=" not in raw:
            raise ValueError(
                "--column-faker must look like column=faker_arabic|faker_english|default"
            )
        column, mode = raw.split("=", 1)
        column = column.strip()
        if not column:
            raise ValueError("--column-faker requires a non-empty column name")
        out[column] = T.canonicalize_faker_locale(mode)
    return out


def _apply_column_faker_overrides(plan, overrides: dict[str, str]) -> None:
    for column, locale in overrides.items():
        rule = plan.rule_for(column)
        if rule is None:
            raise ValueError(f"--column-faker: unknown column {column!r}")
        if rule.strategy != "fake":
            raise ValueError(
                f"--column-faker: column {column!r} uses strategy {rule.strategy!r}, not 'fake'"
            )
        kind = (rule.params or {}).get("kind")
        if kind not in ("name", "address", "company"):
            raise ValueError(
                f"--column-faker: column {column!r} fake kind {kind!r} does not use locale overrides"
            )
        rule.params = dict(rule.params or {})
        rule.params["locale"] = locale


def _run_mask(args) -> int:
    """`redibis mask plan|apply|auto <file>` — de-identify for safe sharing."""
    from redibis.masking import (MaskingPlan, MaskingEngine, RunKeys,
                                  auto_suggest_plan, risk_report)
    from redibis.masking.engine import build_manifest, build_audit_report
    from pathlib import Path as _P
    table = _table_from_file(args.file, args.table)
    df = _load_mask_df(args.file)
    default_locale = _mask_default_locale(args)
    try:
        column_faker_overrides = _parse_column_faker_overrides(getattr(args, "column_faker", None))
    except ValueError as e:
        print(f"mask: {e}", file=sys.stderr)
        return 2

    if args.mask_action == "plan":
        dets = _detect_for_mask(df) if args.from_pii else []
        plan = auto_suggest_plan(table, df.columns.tolist(), dets,
                                 default_locale=default_locale)
        try:
            _apply_column_faker_overrides(plan, column_faker_overrides)
        except ValueError as e:
            print(f"mask: {e}", file=sys.stderr)
            return 2
        out = args.out or "plan.yaml"
        _P(out).write_text(plan.to_yaml(), encoding="utf-8")
        print(f"Wrote masking plan → {out} ({len(plan.columns)} columns, "
              f"source={plan.source})")
        return 0

    # apply / auto both produce a masked export
    if args.mask_action == "apply":
        if not args.plan or not _P(args.plan).is_file():
            print("mask apply: --plan <plan.yaml> is required", file=sys.stderr)
            return 2
        plan = MaskingPlan.from_yaml(_P(args.plan).read_text(encoding="utf-8"))
    else:  # auto
        dets = _detect_for_mask(df)
        plan = auto_suggest_plan(table, df.columns.tolist(), dets,
                                 default_locale=default_locale)
    try:
        _apply_column_faker_overrides(plan, column_faker_overrides)
    except ValueError as e:
        print(f"mask: {e}", file=sys.stderr)
        return 2

    keys = RunKeys.mint(seed=args.seed or plan.seed or None)
    plan.run_id = keys.run_id
    plan.seed = plan.seed or keys.seed
    eng = MaskingEngine(plan, keys)
    masked = eng.transform_dataframe(df)

    out = args.out or f"{_P(args.file).stem}_safe.{ 'parquet' if args.format=='parquet' else 'csv'}"
    if args.format == "parquet":
        masked.to_parquet(out, index=False)
    else:
        masked.to_csv(out, index=False)

    # Manifest sidecar (plan + key references, NEVER keys).
    manifest = build_manifest(
        plan, keys,
        column_run_meta=eng.column_run_meta,
        rows=len(df),
    )
    _P(out + ".manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    audit = build_audit_report(
        plan, keys,
        df=df,
        column_run_meta=eng.column_run_meta,
        export_format=args.format,
        export_filename=_P(out).name,
        source_label=str(args.file),
    )
    audit_path = out + ".audit.json"
    _P(audit_path).write_text(
        json.dumps(audit, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Masked {len(df)} rows → {out}")
    print(f"Manifest → {out}.manifest.json   (keys kept back, NOT exported)")
    print(f"Audit report → {audit_path}")
    s = audit.get("summary") or {}
    print(f"Audit: {s.get('columns_transformed', 0)} columns transformed, "
          f"{s.get('pii_columns_detected', 0)} PII columns, "
          f"risk high={s.get('risk_high', 0)} medium={s.get('risk_medium', 0)}")
    findings = [f for f in risk_report(df, plan) if f["severity"] in ("high", "medium")]
    if findings:
        print("Risk panel:")
        for f in findings:
            print(f"  [{f['severity']}] {f['column']}: {f['issue']}")
    return 0


def _run_mask_capabilities(args) -> int:
    """`redibis mask capabilities` — print transforms.capabilities()."""
    from redibis.masking import transforms as T
    caps = T.capabilities()
    if getattr(args, "json", False):
        print(json.dumps(caps, indent=2))
        return 0
    for k, v in sorted(caps.items()):
        if isinstance(v, list):
            print(f"{k}: {', '.join(str(x) for x in v)}")
        else:
            print(f"{k}: {v}")
    return 0


def _run_pii(args) -> int:
    """``redibis pii regex export|list`` and ``redibis pii ner list|export``."""
    import json
    from pathlib import Path

    from redibis.config import RedibisConfig
    from redibis.pii.export import (
        export_ner_bundle,
        export_ner_models,
        export_regex_catalog,
        list_regex_catalog_summary,
    )
    from redibis.pii.regex_overrides import RegexOverrides

    overrides = None
    if getattr(args, "config", None):
        cfg = RedibisConfig.from_yaml(args.config)
        if cfg.pii.regex_overrides:
            overrides = RegexOverrides.from_dict(cfg.pii.regex_overrides)

    if args.pii_action == "regex":
        if args.regex_action == "export":
            text = export_regex_catalog(
                overrides=overrides,
                active_only=getattr(args, "active_only", False),
                fmt=getattr(args, "format", "json"),
            )
            out = getattr(args, "out", None)
            if out:
                Path(out).write_text(text, encoding="utf-8")
                print(f"Wrote regex catalog to {out}")
            else:
                print(text)
            return 0
        if args.regex_action == "list":
            rows = list_regex_catalog_summary(
                overrides=overrides,
                active_only=getattr(args, "active_only", True),
            )
            if getattr(args, "json", False):
                print(json.dumps(rows, indent=2))
                return 0
            print(f"{'name':<36} {'entity_type':<20} {'group':<12} active")
            print("-" * 80)
            for r in rows:
                print(
                    f"{r['name']:<36} {r['entity_type']:<20} "
                    f"{r['group']:<12} {r['active']}"
                )
            print(f"\n{len(rows)} patterns")
            return 0

    if args.pii_action == "ner":
        models_dir = getattr(args, "models_dir", None)
        if getattr(args, "config", None) and not models_dir:
            models_dir = RedibisConfig.from_yaml(args.config).pii.models_dir
        if args.ner_action == "list":
            specs = export_ner_models(models_dir)
            if getattr(args, "json", False):
                print(json.dumps(specs, indent=2))
                return 0
            print(f"{'name':<24} {'type':<8} labels")
            print("-" * 72)
            for s in specs:
                labels = ", ".join((s.get("labels") or [])[:6])
                if len(s.get("labels") or []) > 6:
                    labels += "…"
                print(f"{s.get('name', ''):<24} {s.get('type', ''):<8} {labels}")
            print(f"\n{len(specs)} models")
            return 0
        if args.ner_action == "export":
            specs = export_ner_models(models_dir)
            out = Path(getattr(args, "out", None) or ".")
            out.mkdir(parents=True, exist_ok=True)
            (out / "ner_models.json").write_text(
                json.dumps(specs, indent=2, ensure_ascii=False), encoding="utf-8",
            )
            if getattr(args, "bundle", None):
                dest = export_ner_bundle(args.bundle, out, models_dir=models_dir)
                print(f"Bundled model to {dest}")
            print(f"Wrote NER specs to {out / 'ner_models.json'}")
            return 0

    if args.pii_action == "text":
        return _run_pii_text(args)
    if args.pii_action == "deid":
        return _run_pii_deid(args)
    if args.pii_action in ("calibrate-nid", "calibrate"):
        return _run_pii_calibrate(args)

    print("unknown pii subcommand", file=sys.stderr)
    return 2


def _run_pii_calibrate(args) -> int:
    """``redibis pii calibrate --entity nid|imei|imsi|geo`` (calibrate-nid alias)."""
    entity = getattr(args, "entity", None) or "nid"
    if args.pii_action == "calibrate-nid":
        entity = "nid"
    entity = str(entity).lower()
    if entity == "nid":
        return _run_pii_calibrate_nid(args)
    if entity == "imei":
        return _run_pii_calibrate_imei(args)
    if entity == "imsi":
        return _run_pii_calibrate_imsi(args)
    if entity == "geo":
        return _run_pii_calibrate_geo(args)
    print(f"calibrate: unknown entity {entity!r}", file=sys.stderr)
    return 2


def _run_pii_calibrate_imei(args) -> int:
    from pathlib import Path
    import pandas as pd
    from redibis.pii.device_id import validate_imei, normalize_imei

    path = Path(args.input)
    df = pd.read_csv(path)
    col = args.column
    values = df[col].tolist()
    stages = {"normalize": 0, "format": 0, "luhn": 0, "rbi": 0}
    non_null = 0
    for raw in values:
        if raw is None or str(raw).strip() == "":
            continue
        non_null += 1
        digits = normalize_imei(raw)
        if not digits:
            continue
        stages["normalize"] += 1
        if len(digits) != 15:
            continue
        stages["format"] += 1
        r = validate_imei(digits, verify_rbi=False)
        if r.luhn_ok:
            stages["luhn"] += 1
        r2 = validate_imei(digits, verify_rbi=True)
        if r2.valid:
            stages["rbi"] += 1
    print(f"IMEI calibration — {path.name}.{col}   ({non_null:,} non-null values)")
    print()
    print(f"  {'stage':<16} {'passed':>8}")
    for s in ("normalize", "format", "luhn", "rbi"):
        print(f"  {s:<16} {stages[s]:>8,}")
    return 0


def _run_pii_calibrate_imsi(args) -> int:
    from pathlib import Path
    import pandas as pd
    from redibis.pii.subscriber_id import validate_imsi, normalize_imsi

    path = Path(args.input)
    df = pd.read_csv(path)
    col = args.column
    values = df[col].tolist()
    stages = {"normalize": 0, "format": 0, "mcc_known": 0, "home": 0, "roaming": 0}
    non_null = 0
    for raw in values:
        if raw is None or str(raw).strip() == "":
            continue
        non_null += 1
        digits = normalize_imsi(raw)
        if not digits:
            continue
        stages["normalize"] += 1
        if len(digits) not in (14, 15):
            continue
        stages["format"] += 1
        r = validate_imsi(digits)
        if r.valid:
            stages["mcc_known"] += 1
            if r.is_home:
                stages["home"] += 1
            else:
                stages["roaming"] += 1
    print(f"IMSI calibration — {path.name}.{col}   ({non_null:,} non-null values)")
    print()
    print(f"  {'stage':<16} {'passed':>8}")
    for s in ("normalize", "format", "mcc_known", "home", "roaming"):
        print(f"  {s:<16} {stages[s]:>8,}")
    return 0


def _run_pii_calibrate_geo(args) -> int:
    from pathlib import Path
    from dataclasses import replace
    import pandas as pd
    from redibis.pii.geo_engine import (
        geo_column_signals, geo_verdict, geo_confidence, name_has_geo_token,
    )

    path = Path(args.input)
    df = pd.read_csv(path)
    col = args.column
    partner = getattr(args, "partner_column", None)
    partner_values = df[partner].tolist() if partner and partner in df.columns else None
    values = df[col].tolist()
    sig = geo_column_signals(values, partner_values=partner_values)
    sig = replace(
        sig,
        name_signal=name_has_geo_token(col),
        partner_signal=partner_values is not None,
    )
    verdict = geo_verdict(sig)
    conf = geo_confidence(sig)
    print(f"GEO calibration — {path.name}.{col}"
          + (f" (partner={partner})" if partner else " (no partner)"))
    print(f"  range_signal={sig.range_signal}  verdict={verdict}  confidence={conf:.4f}")
    print(f"  geofence_rate={sig.geofence_rate:.4f}  egypt_hits={sig.egypt_geofence_hits}")
    print(f"  decimal_places_median={sig.decimal_places_median:.2f}  "
          f"precision={sig.precision_signal}")
    return 0


def _run_pii_calibrate_nid(args) -> int:
    """``redibis pii calibrate-nid`` — per-stage EG NID funnel on a CSV column."""
    from pathlib import Path

    import pandas as pd

    from redibis.config import RedibisConfig
    from redibis.pii.national_id_egypt import calibrate_nid_funnel

    path = Path(args.input)
    if not path.is_file():
        print(f"calibrate-nid: input not found: {path}", file=sys.stderr)
        return 2
    col = args.column
    df = pd.read_csv(path)
    if col not in df.columns:
        print(
            f"calibrate-nid: column {col!r} not in {list(df.columns)}",
            file=sys.stderr,
        )
        return 2

    series = df[col]
    sample_n = getattr(args, "sample", None)
    if sample_n:
        series = series.dropna().sample(
            n=min(int(sample_n), series.dropna().shape[0]),
            random_state=42,
        )
    values = series.tolist()

    cfg = (
        RedibisConfig.from_yaml(args.config)
        if getattr(args, "config", None)
        else RedibisConfig()
    )
    nid_cfg = cfg.pii.national_id_egypt
    funnel = calibrate_nid_funnel(
        values,
        max_age=nid_cfg.max_age,
        min_birth_year=nid_cfg.min_birth_year,
    )
    non_null = funnel["non_null"]
    stages = funnel["stages"]
    label = f"{path.name}.{col}"

    print(f"EG National ID calibration — {label}   ({non_null:,} non-null values)")
    print()
    print(f"  {'stage':<16} {'passed':>8}  {'rate':>8}  {'cumulative':>12}")
    prev = max(non_null, 1)
    cum = 1.0
    order = (
        "normalize", "format", "calendar", "governorate", "plausibility",
    )
    for stage in order:
        passed = stages[stage]
        rate = (passed / prev) if prev else 0.0
        cum = (passed / max(non_null, 1)) if non_null else 0.0
        print(f"  {stage:<16} {passed:>8,}  {rate:>8.4f}  {cum:>12.4f}")
        prev = max(passed, 1) if passed else prev

    print("  " + "─" * 49)
    cd_passed = stages["checkdigit"]
    cd_base = max(stages["plausibility"], 1)
    cd_rate = cd_passed / cd_base if stages["plausibility"] else 0.0
    cd_cum = cd_passed / max(non_null, 1) if non_null else 0.0
    print(
        f"  {'check digit':<16} {cd_passed:>8,}  {cd_rate:>8.4f}  {cd_cum:>12.4f}",
        end="",
    )
    if stages["plausibility"] and cd_rate <= 0.20:
        print("   ← ~1/11: ALGORITHM DOES NOT HOLD")
    else:
        print()

    print()
    if cd_rate >= 0.98 and stages["plausibility"] > 0:
        print("  Recommendation: safe to enable `verify_check_digit`.")
    elif cd_rate <= 0.20:
        print(
            "  Recommendation: keep verify_check_digit=false.\n"
            "  Algorithm does not hold for this population; keep disabled."
        )
    else:
        print("  Recommendation: inconclusive — do not enable `verify_check_digit`.")

    obs = (
        stages["plausibility"] / max(stages["format"], 1)
        if stages["format"]
        else 0.0
    )
    if obs >= 0.90:
        suggested = max(0.85, round(obs - 0.05, 2))
        print(
            f"  Suggested valid_rate_min: {suggested:.2f}  "
            f"(observed {obs:.3f}, margin 0.05)"
        )
    return 0


def _pii_text_input(args) -> str:
    """Resolve text from positional / --file / stdin."""
    if getattr(args, "file", None):
        from pathlib import Path
        path = args.file
        if path == "-":
            return sys.stdin.read()
        return Path(path).read_text(encoding="utf-8")
    text = getattr(args, "text", None)
    if text == "-":
        return sys.stdin.read()
    if text:
        return text
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise SystemExit("pii text: provide TEXT, --file, or stdin")


def _run_pii_text(args) -> int:
    """``redibis pii text`` — free-text span scan."""
    import json
    from redibis.config import RedibisConfig
    from redibis.services.text_pii_service import TextPIIService

    cfg = RedibisConfig.from_yaml(args.config) if getattr(args, "config", None) else RedibisConfig()
    svc = TextPIIService(redibis_config=cfg)
    text = _pii_text_input(args)
    entities = ()
    if getattr(args, "entities", None):
        entities = tuple(e.strip() for e in args.entities.split(",") if e.strip())
    result = svc.scan(
        text,
        language=getattr(args, "language", "en") or "en",
        engines=getattr(args, "engines", "both") or "both",
        min_score=float(getattr(args, "min_score", 0.35)),
        return_text=not getattr(args, "no_text", False),
        resolve=getattr(args, "resolve", "priority") or "priority",
        use_llm=bool(getattr(args, "use_llm", False)),
        entities=list(entities),
    )
    if getattr(args, "redact", False):
        print(svc.redact(text, result))
        return 0
    as_json = bool(getattr(args, "json", False)) or getattr(args, "format", None) == "json"
    if as_json:
        print(json.dumps(result.to_dict(return_text=not getattr(args, "no_text", False)), indent=2, ensure_ascii=False))
        return 0
    # table (default)
    print(f"{'ENTITY':<18} {'START':>5} {'END':>5} {'SCORE':>6} {'ENGINE':<8} TEXT")
    print("-" * 80)
    for s in result.detections:
        txt = (s.text or "")[:40]
        prop = " *" if s.is_proposal else ""
        print(
            f"{s.entity_type:<18} {s.start or 0:>5} {s.end or 0:>5} "
            f"{s.score:>6.2f} {s.engine:<8} {txt}{prop}"
        )
    print(f"\n{len(result.detections)} span(s)  engines={list(result.engines_ran)}")
    return 0


def _run_pii_deid(args) -> int:
    """``redibis pii deid`` — scan + apply de-identification policy."""
    import json
    from pathlib import Path
    from redibis.config import RedibisConfig
    from redibis.pii.deid.policy import DeidPolicy, EntityRule
    from redibis.services.text_pii_service import TextPIIService, TextPIIServiceError

    cfg = RedibisConfig.from_yaml(args.config) if getattr(args, "config", None) else RedibisConfig()
    svc = TextPIIService(redibis_config=cfg)
    svc.register_policy(DeidPolicy.redact_all())
    text = _pii_text_input(args)

    policy = None
    if getattr(args, "policy_file", None):
        policy = DeidPolicy.from_yaml(Path(args.policy_file).read_text(encoding="utf-8"))
    elif getattr(args, "policy", None):
        policy = svc.get_policy(args.policy)
        if policy is None and args.policy == "full-redact":
            policy = DeidPolicy.redact_all()
            svc.register_policy(policy)
    elif getattr(args, "default", None):
        overrides = []
        for item in getattr(args, "set", None) or []:
            if "=" not in item:
                continue
            et, strat = item.split("=", 1)
            overrides.append(EntityRule(entity_type=et.strip().upper(), strategy=strat.strip()))
        policy = DeidPolicy(
            id="adhoc",
            default=EntityRule(entity_type="*", strategy=args.default),
            overrides=tuple(overrides),
        )
    else:
        print("pii deid: require --policy, --policy-file, or --default STRATEGY", file=sys.stderr)
        return 2

    try:
        _result, deid = svc.deidentify(text, policy=policy, policy_id=None)
    except TextPIIServiceError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    out_text = deid.deidentified_text
    out_path = getattr(args, "out", None)
    if out_path:
        Path(out_path).write_text(out_text, encoding="utf-8")
        print(f"Wrote de-identified text to {out_path}")
    else:
        print(out_text)
    if getattr(args, "json", False):
        print(json.dumps(deid.to_dict(), indent=2, ensure_ascii=False), file=sys.stderr)
    return 0


def _run_mask_regex(args) -> int:
    """`redibis mask regex list|test` — inspect regex fake-data patterns."""
    from redibis.masking.regex_library import list_patterns, resolve_pattern
    from redibis.masking.engine import RunKeys
    from redibis.masking import transforms as T

    if args.regex_action == "list":
        patterns = list_patterns()
        if getattr(args, "json", False):
            print(json.dumps(patterns, indent=2, ensure_ascii=False))
            return 0
        print(f"{'name':<18} {'label':<22} {'category':<12} regex")
        print("-" * 72)
        for p in patterns:
            print(f"{p['name']:<18} {p['label']:<22} {p['category']:<12} {p['regex']}")
        print(f"\n{len(patterns)} patterns "
              f"(override via REDIBIS_REGEX_PATTERNS or ./regex_patterns.json)")
        return 0

    pattern = (args.pattern or "").strip()
    if not pattern and args.library:
        pattern = resolve_pattern(args.library) or ""
    if not pattern:
        print("mask regex test: --pattern or --library is required", file=sys.stderr)
        return 2
    keys = RunKeys.mint(seed=args.seed or "regex-test")
    mk = keys.master_key
    n = max(1, min(int(args.n), 20))
    print(f"pattern: {pattern}")
    for i in range(n):
        rng = T._value_rng(mk, "k1", "sample", str(i), args.deterministic)
        print(T.fake_from_regex(rng, pattern, deterministic=args.deterministic))
    caps = T.capabilities()
    if not args.deterministic and not caps.get("rstr"):
        print("(install rstr for richer non-deterministic patterns: pip install rstr)",
              file=sys.stderr)
    return 0


def _run_deep_scan(args) -> int:
    """``redibis deep-scan <file>`` — evidence fan-out (no contract write)."""
    from datetime import datetime, timezone
    from pathlib import Path

    import pandas as pd

    from redibis.agents.deep_scan import run_deep_scan
    from redibis.config import RedibisConfig
    from redibis.telemetry.init import set_run_dir

    table = args.table
    path = Path(args.file)
    if not table:
        stem = path.stem
        table = stem if "." in stem else f"default.{stem}"

    config = (
        RedibisConfig.from_yaml(args.config)
        if getattr(args, "config", None)
        else RedibisConfig.default()
    )
    if not config.table:
        config.table = table

    run_id = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    run_dir = Path(args.output_dir) / run_id
    set_run_dir(run_dir)

    suffix = path.suffix.lower()
    if suffix == ".parquet":
        df = pd.read_parquet(path)
    else:
        df = pd.read_csv(path)

    producers = None
    if getattr(args, "producers", None):
        producers = [p.strip() for p in args.producers.split(",") if p.strip()]

    build_bundle = not getattr(args, "no_bundle", False)
    if getattr(args, "bundle", False):
        build_bundle = True

    result = run_deep_scan(
        table,
        run_id,
        df,
        producers=producers,
        run_dir=run_dir,
        config=config,
        build_bundle=build_bundle,
    )

    print(f"\nDeep scan complete for {table}")
    print(f"  run_id={run_id}")
    print(f"  run_dir={run_dir}")
    print(f"  producers_ok={len(result.get('evidence') or [])}")
    if result.get("errors"):
        print(f"  errors={len(result['errors'])}")
        for err in result["errors"][:5]:
            print(f"    - {err}")
    if result.get("bundle"):
        print(f"  bundle={result['bundle']}")
    return 0 if not result.get("errors") else 1


def _run_scan(args, store: ContractStore, backend):
    """``redibis scan <file>`` — session/run layout + optional auto_write to contract store."""
    from redibis.cli.overrides import apply_cli_overrides
    from redibis.config import GlinerConfig, NERConfig, RedibisConfig
    from redibis.services.code_scan_session import CodeScanSession
    from redibis.services.scan_service import ScanConfig, to_scan_config
    from dataclasses import replace
    from pathlib import Path as _P

    table = args.table
    if not table:
        stem = _P(args.file).stem
        table = stem if "." in stem else f"default.{stem}"

    if getattr(args, "config", None) or _scan_tier_flags_set(args):
        rb_config = (
            RedibisConfig.from_yaml(args.config)
            if getattr(args, "config", None)
            else RedibisConfig.default()
        )
        apply_cli_overrides(rb_config, args)
        if not rb_config.table:
            rb_config.table = table
        scan_config = to_scan_config(
            rb_config,
            output_dir=getattr(args, "scan_output_dir", None) or args.output_dir,
        )
        if getattr(args, "no_phonenumbers", False):
            from dataclasses import replace
            scan_config = replace(scan_config, use_phonenumbers=False)
        redibis_config = rb_config
        if rb_config.memory.enabled:
            store = ContractStore(
                backend,
                bucket=args.s3_contracts_bucket,
                memory_config=rb_config.memory,
            )
    else:
        run_pii, run_profile, run_quality = parse_scan_mode(args.mode)
        ner_path = (getattr(args, "ner_model", None) or getattr(args, "gliner_model", None) or "").strip()
        ner_labels_raw = (getattr(args, "ner_labels", None) or "").strip()
        ner_labels = [x.strip() for x in ner_labels_raw.split(",") if x.strip()] if ner_labels_raw else []
        scan_config = ScanConfig(
            table=table,
            equation_mode=args.equation,
            run_pii=run_pii,
            run_profile=run_profile,
            run_quality=run_quality,
            profiler_engine=getattr(args, "profiler_engine", None) or "great_expectations",
            pii_engines=args.pii_engines,
            generate_ge_docs=not args.no_ge_docs,
            validate_contracts=not args.no_validate,
            automerge=args.automerge or "none",
            output_dir=_P(getattr(args, "scan_output_dir", None) or args.output_dir),
            ner_config=NERConfig(model_path=ner_path, labels=ner_labels),
            gliner_config=GlinerConfig(model_id=ner_path),
            use_phonenumbers=False if getattr(args, "no_phonenumbers", False) else None,
        )
        if getattr(args, "profiler_engine", None):
            scan_config = replace(scan_config, profiler_engine=args.profiler_engine)
        redibis_config = None

    sub_store = _build_subcontract_store(args, backend)
    output_root = _P(getattr(args, "scan_output_dir", None) or args.output_dir)

    session = CodeScanSession.create(
        _P(args.file),
        table=scan_config.table,
        output_root=output_root,
        backend=backend,
        store=store,
        runs_bucket=getattr(args, "s3_runs_bucket", "pii-reports"),
        sub_store=sub_store,
        session_id=getattr(args, "session_id", None),
    )
    result = session.scan(
        mode=args.mode,
        automerge=scan_config.automerge,
        config=scan_config,
        redibis_config=redibis_config,
    )
    artifacts_dir = session.session_dir / "runs" / result.run_id / "artifacts"
    print(f"\nScan {result.status} for {scan_config.table}")
    print(f"  session_id={session.session_id}")
    print(f"  run_id={result.run_id}")
    print(f"  artifacts={artifacts_dir}")
    print(f"  rows={result.total_rows} cols={result.total_columns}")
    run_pii, run_profile, run_quality = parse_scan_mode(args.mode)
    if run_quality:
        print(f"  quality: {result.quality_passed}/{result.quality_expectations} passed"
              + (f" → merged v{result.quality_contract_version}"
                 if result.quality_contract_version else " (in quality-contracts bucket)"))
    elif run_profile and result.profile:
        print(f"  profile: {result.total_columns} columns triaged")
    if run_pii:
        print(f"  pii: {result.pii_columns_detected}/{result.pii_columns_scanned} detected"
              + (f" → merged v{result.pii_contract_version}"
                 if result.pii_contract_version else " (in pii-contracts bucket)"))
    if scan_config.automerge == "none":
        print(f"\nReview & merge with:  redibis runs list {scan_config.table} --kind pii|quality")
    return 0 if result.status == "success" else 1


def _run_config_dump(args) -> int:
    from redibis.config import RedibisConfig

    if args.output:
        RedibisConfig.dump_default_yaml(args.output)
        print(f"Wrote default config to {args.output}")
    else:
        import sys
        import yaml
        sys.stdout.write(yaml.safe_dump(
            RedibisConfig.default().to_dict(),
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
        ))
    return 0


def _run_enrich_export_context(args) -> int:
    from redibis.config import RedibisConfig
    from redibis.enrich.context_profile import export_context_profile

    dest = args.table
    if not dest:
        print("enrich export-context: DIR is required", file=sys.stderr)
        return 2
    cfg_path = getattr(args, "config", None) or __import__("os").environ.get("REDIBIS_CONFIG")
    redibis_cfg = RedibisConfig.from_yaml(cfg_path) if cfg_path else RedibisConfig.default()
    configured = getattr(getattr(redibis_cfg.enrich, "context", None), "pack", "") or ""
    custom_dir = getattr(getattr(redibis_cfg.enrich, "context", None), "custom_dir", "") or ""
    try:
        manifest = export_context_profile(
            dest,
            pack_path=getattr(args, "pack", None),
            include_custom=bool(getattr(args, "include_custom", False)),
            custom_dir=custom_dir or None,
            configured_pack=configured,
        )
    except Exception as exc:
        print(f"enrich export-context: {exc}", file=sys.stderr)
        return 1
    print(f"Exported context profile → {dest}")
    print(f"  files: {len(manifest.get('files') or [])}")
    print(f"  include_custom: {manifest.get('include_custom')}")
    return 0


def _context_file_args(args) -> list[str]:
    files = []
    if getattr(args, "table", None):
        files.append(args.table)
    files.extend(getattr(args, "context_files", None) or [])
    return files


def _run_enrich_add_context(args, store: ContractStore) -> int:
    from redibis.config import RedibisConfig
    from redibis.enrich.context_profile import CustomContextStore
    from redibis.enrich.service import enrichment_service_for_store

    files = _context_file_args(args)
    if not files:
        print("enrich add-context: FILE is required", file=sys.stderr)
        return 2
    scope = getattr(args, "scope", None) or "global"
    mode = getattr(args, "context_mode", None) or "shared"
    stage = getattr(args, "context_stage", None) or ""
    try:
        if scope == "global":
            cfg_path = getattr(args, "config", None) or __import__("os").environ.get("REDIBIS_CONFIG")
            redibis_cfg = RedibisConfig.from_yaml(cfg_path) if cfg_path else RedibisConfig.default()
            custom_dir = getattr(getattr(redibis_cfg.enrich, "context", None), "custom_dir", "") or ""
            written = CustomContextStore(custom_dir or None).add(
                [Path(f) for f in files], mode=mode, stage=stage,
            )
            for item in written:
                print(f"added global {item['path']}")
            return 0
        table = getattr(args, "context_table", None)
        if not table:
            print("enrich add-context: --table is required with --scope table", file=sys.stderr)
            return 2
        svc = enrichment_service_for_store(store)
        for path in files:
            p = Path(path)
            if not p.is_file():
                print(f"enrich add-context: not a file: {p}", file=sys.stderr)
                return 2
            key = svc.add_context_doc(table, p.name, p.read_bytes(), mode=mode, stage=stage)
            print(f"added table {table}: {key}")
        return 0
    except (ValueError, FileNotFoundError) as exc:
        print(f"enrich add-context: {exc}", file=sys.stderr)
        return 2


def _run_enrich_list_context(args, store: ContractStore) -> int:
    from redibis.config import RedibisConfig
    from redibis.enrich.context_profile import CustomContextStore
    from redibis.enrich.service import enrichment_service_for_store

    scope = getattr(args, "scope", None) or "global"
    items: list[dict] = []
    if scope == "global":
        cfg_path = getattr(args, "config", None) or __import__("os").environ.get("REDIBIS_CONFIG")
        redibis_cfg = RedibisConfig.from_yaml(cfg_path) if cfg_path else RedibisConfig.default()
        custom_dir = getattr(getattr(redibis_cfg.enrich, "context", None), "custom_dir", "") or ""
        items = CustomContextStore(custom_dir or None).list_files()
    else:
        table = getattr(args, "context_table", None) or getattr(args, "table", None)
        if not table:
            print("enrich list-context: --table is required with --scope table", file=sys.stderr)
            return 2
        svc = enrichment_service_for_store(store)
        items = [{"path": n} for n in svc.list_context_docs(table)]
    if getattr(args, "context_json", False):
        print(json.dumps({"scope": scope, "files": items}, indent=2))
    else:
        if not items:
            print("(none)")
        for item in items:
            print(item.get("path") or item)
    return 0


def _run_enrich_remove_context(args, store: ContractStore) -> int:
    from redibis.config import RedibisConfig
    from redibis.enrich.context_profile import CustomContextStore
    from redibis.enrich.service import enrichment_service_for_store

    files = _context_file_args(args)
    if not files:
        print("enrich remove-context: PATH is required", file=sys.stderr)
        return 2
    scope = getattr(args, "scope", None) or "global"
    try:
        if scope == "global":
            cfg_path = getattr(args, "config", None) or __import__("os").environ.get("REDIBIS_CONFIG")
            redibis_cfg = RedibisConfig.from_yaml(cfg_path) if cfg_path else RedibisConfig.default()
            custom_dir = getattr(getattr(redibis_cfg.enrich, "context", None), "custom_dir", "") or ""
            store_c = CustomContextStore(custom_dir or None)
            for rel in files:
                ok = store_c.remove(rel)
                print(f"{'removed' if ok else 'not found'}: {rel}")
            return 0
        table = getattr(args, "context_table", None)
        if not table:
            print("enrich remove-context: --table is required with --scope table", file=sys.stderr)
            return 2
        svc = enrichment_service_for_store(store)
        for name in files:
            ok = svc.delete_context_doc(table, name)
            print(f"{'removed' if ok else 'not found'}: {name}")
        return 0
    except ValueError as exc:
        print(f"enrich remove-context: {exc}", file=sys.stderr)
        return 2


def _run_enrich(args, store: ContractStore):
    """`redibis enrich <table> --provider ...` — auto-writes LLM contract to active."""
    action = getattr(args, "enrich_action", "run") or "run"
    if action == "export-context":
        return _run_enrich_export_context(args)
    if action == "add-context":
        return _run_enrich_add_context(args, store)
    if action == "list-context":
        return _run_enrich_list_context(args, store)
    if action == "remove-context":
        return _run_enrich_remove_context(args, store)
    from datetime import datetime, timezone
    import os

    from redibis.config import RedibisConfig
    from redibis.enrich.service import enrichment_service_for_store
    from redibis.enrich.providers import get_provider, list_providers
    from redibis.store.run_output_writer import RunOutputWriter

    config_path = getattr(args, "providers_file", None)
    cfg_path = getattr(args, "config", None) or os.environ.get("REDIBIS_CONFIG")
    redibis_cfg = (
        RedibisConfig.from_yaml(cfg_path)
        if cfg_path
        else RedibisConfig.default()
    )
    if getattr(args, "list_providers", False):
        for p in list_providers(config_path):
            key = " (needs key)" if p["needs_key"] else ""
            base = f"  base={p['api_base']}" if p.get("api_base") else ""
            print(f"{p['name']:<12} {p['model']}{key}{base}")
            if p["description"]:
                print(f"             {p['description']}")
        return 0

    extra = list(getattr(args, "context_files", None) or [])
    if extra:
        print(
            f"enrich: unexpected extra arguments: {' '.join(extra)}",
            file=sys.stderr,
        )
        return 2

    pack_path = getattr(args, "pack", None) or (
        getattr(getattr(redibis_cfg.enrich, "context", None), "pack", "") or None
    )
    if pack_path and getattr(args, "prompt", None):
        print(
            "--prompt cannot be combined with --pack.\n"
            "Move domain instructions into the pack or use --instructions "
            "for run-specific guidance.",
            file=sys.stderr,
        )
        return 2

    enrichment_pack = None
    if pack_path:
        from redibis.enrich.packs.errors import PackError
        from redibis.enrich.packs.validator import load_and_validate_pack

        try:
            enrichment_pack, _report = load_and_validate_pack(pack_path)
        except PackError as exc:
            print(f"enrich: pack error: {exc}", file=sys.stderr)
            for err in getattr(exc, "errors", None) or []:
                print(f"  - {err}", file=sys.stderr)
            return 2

    if not args.table and not getattr(args, "contract_file", None):
        print(
            "enrich: a table is required (or use --list-providers / --contract)",
            file=sys.stderr,
        )
        return 2

    input_contract = None
    if getattr(args, "contract_file", None):
        from redibis.enrich.context import load_contract_for_enrichment

        try:
            input_contract, resolved_table = load_contract_for_enrichment(
                args.contract_file,
                table=args.table,
            )
        except (FileNotFoundError, ValueError) as exc:
            print(f"enrich: {exc}", file=sys.stderr)
            return 2
        if args.table and args.table != resolved_table:
            print(
                f"enrich: --table {args.table!r} overrides contract identity "
                f"{resolved_table!r} from file",
                file=sys.stderr,
            )
        args.table = args.table or resolved_table

    if not args.table:
        print("enrich: could not resolve table name", file=sys.stderr)
        return 2

    if input_contract is None and store.get_active(args.table) is None:
        print(
            f"No active contract for {args.table!r} — pass --contract FILE "
            "or upsert a contract first.",
            file=sys.stderr,
        )
        return 1

    svc = enrichment_service_for_store(store)
    for path in (args.context or []):
        p = Path(path)
        if p.is_file():
            svc.add_context_doc(args.table, p.name, p.read_bytes())
    for path in (args.example_docs or []):
        p = Path(path)
        if p.is_file():
            svc.add_example_doc(args.table, p.name, p.read_bytes())
    system_prompt = None
    if args.prompt and Path(args.prompt).is_file():
        system_prompt = Path(args.prompt).read_text(encoding="utf-8")
    extra_instructions = None
    if args.instructions:
        extra_instructions = (
            Path(args.instructions).read_text(encoding="utf-8")
            if Path(args.instructions).is_file()
            else args.instructions
        )
    provider = get_provider(
        args.provider,
        model=args.model,
        api_key=args.api_key,
        endpoint_url=args.endpoint,
        config_path=config_path,
    )

    approve_id = getattr(args, "approve_context_reduction", None)
    dry_run = bool(getattr(args, "dry_run", False))

    if dry_run:
        from redibis.enrich.packs.errors import ContextReductionRequired

        try:
            ctx = svc.build_context(
                args.table,
                provider,
                system_prompt=system_prompt,
                extra_instructions=extra_instructions,
                example_contracts=args.examples or None,
                external_masked_acknowledged=getattr(args, "external_masked_ack", False),
                redibis_config=redibis_cfg,
                input_contract=input_contract,
                enrichment_pack=enrichment_pack,
                approve_context_reduction=approve_id,
                reduction_approval_source="cli_flag" if approve_id else "dry_run",
                enforce_pack_reduction_approval=False,
            )
        except ContextReductionRequired as exc:
            print(str(exc), file=sys.stderr)
            print(json.dumps({"reduction_plan": getattr(exc, "plan", {}) or {}}, indent=2))
            return 2
        except Exception as exc:
            print(f"enrich dry-run failed: {exc}", file=sys.stderr)
            return 1
        print("=== DRY RUN (no LLM call, no write) ===")
        if ctx.enrichment_pack:
            print(
                f"pack: {ctx.enrichment_pack.get('identity')} "
                f"sha256={ctx.enrichment_pack.get('sha256')}"
            )
        red = (ctx.pack_context_provenance or {}).get("context_reduction") or {}
        if red.get("required") and not red.get("applied"):
            print("Context reduction REQUIRED before a real enrich run:")
            print(f"  plan_id: {red.get('reduction_plan_id')}")
            print("  re-run with --approve-context-reduction <plan_id>")
        print(
            f"system_prompt_chars={len(ctx.system_prompt)} "
            f"user_prompt_chars={len(ctx.user_prompt)}"
        )
        print("--- SYSTEM ---")
        print(ctx.system_prompt)
        print("--- USER ---")
        print(ctx.user_prompt)
        return 0

    run_id = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    run_writer = RunOutputWriter(
        backend=store.backend,
        bucket=getattr(args, "s3_runs_bucket", "pii-reports"),
        workflow="enrich",
        table=args.table,
        run_id=run_id,
    )
    try:
        if enrichment_pack is not None and not approve_id and sys.stdin.isatty():
            from redibis.enrich.packs.context_compiler import compile_pack_context

            probe = svc.build_context(
                args.table,
                provider,
                system_prompt=system_prompt,
                extra_instructions=extra_instructions,
                example_contracts=args.examples or None,
                external_masked_acknowledged=getattr(args, "external_masked_ack", False),
                redibis_config=redibis_cfg,
                input_contract=input_contract,
                enrichment_pack=enrichment_pack,
                enforce_pack_reduction_approval=False,
            )
            red = (probe.pack_context_provenance or {}).get("context_reduction") or {}
            if red.get("required") and not red.get("applied"):
                plan_id = red.get("reduction_plan_id")
                contract = input_contract or store.get_active(args.table) or {}
                compiled = compile_pack_context(
                    enrichment_pack,
                    contract,
                    provider_name=provider.name or "demo",
                    model=provider.model or "",
                    extra_instructions=extra_instructions or "",
                )
                if compiled.reduction_plan:
                    print(compiled.reduction_plan.summary)
                answer = input("Approve this exact reduction plan? [y/N] ").strip().lower()
                if answer not in ("y", "yes"):
                    print("Aborted: context reduction not approved.", file=sys.stderr)
                    return 2
                approve_id = plan_id

        if getattr(args, "multistep", False) or bool(
            getattr(getattr(redibis_cfg, "enrich", None), "multistep", None)
            and getattr(redibis_cfg.enrich.multistep, "enabled", False)
        ):
            from redibis.enrich.multistep import MultistepEnrichmentRunner

            runner = MultistepEnrichmentRunner.from_config(
                svc,
                provider,
                redibis_config=redibis_cfg,
                steps_file=getattr(args, "steps_file", None),
                enriched_by="cli",
                external_masked_acknowledged=getattr(args, "external_masked_ack", False),
                bypass_rai=getattr(args, "bypass_rai", False),
                run_writer=run_writer,
                run_id=run_id,
                input_contract=input_contract,
                enrichment_pack=enrichment_pack,
                system_prompt=system_prompt,
                extra_instructions=extra_instructions,
                example_contracts=args.examples or None,
                approve_context_reduction=approve_id,
                reduction_approval_source=(
                    "interactive_cli" if (approve_id and sys.stdin.isatty()) else "cli_flag"
                ),
            )
            result = runner.run(args.table)
        else:
            result = svc.enrich(
                args.table,
                provider,
                system_prompt=system_prompt,
                extra_instructions=extra_instructions,
                example_contracts=args.examples or None,
                enriched_by="cli",
                external_masked_acknowledged=getattr(args, "external_masked_ack", False),
                bypass_rai=getattr(args, "bypass_rai", False),
                redibis_config=redibis_cfg,
                run_writer=run_writer,
                run_id=run_id,
                input_contract=input_contract,
                enrichment_pack=enrichment_pack,
                approve_context_reduction=approve_id,
                reduction_approval_source=(
                    "interactive_cli" if (approve_id and sys.stdin.isatty()) else "cli_flag"
                ),
            )
    except PermissionError as exc:
        print(f"RAI policy blocked enrichment: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        from redibis.enrich.packs.errors import ContextReductionRequired, PackError

        if isinstance(exc, ContextReductionRequired):
            print(str(exc), file=sys.stderr)
            return 2
        if isinstance(exc, PackError):
            print(f"enrich: pack error: {exc}", file=sys.stderr)
            return 2
        raise
    print(f"Enrichment for {args.table}: "
          f"{'VALID ✓' if result.valid else 'INVALID ✗'}")
    if result.auto_written:
        print(f"Auto-written to active → v{result.version_after}")
        if result.run_artifacts:
            print(f"Run artifacts: {', '.join(sorted(result.run_artifacts))}")
    rai = (result.enrichment_meta or {}).get("rai")
    if rai and rai.get("advisory_count"):
        print(f"RAI advisories ({rai.get('advisory_count')}): see enrichment_meta.rai")
    pack_meta = (result.enrichment_meta or {}).get("pack")
    if pack_meta:
        print(f"Pack: {pack_meta.get('identity')} sha256={pack_meta.get('sha256')}")
    for e in result.errors:
        print(f"  - {e}", file=sys.stderr)
    if getattr(args, "automerge", False) and not result.auto_written:
        if not result.valid:
            print("Refusing to merge invalid enrichment (fails closed).", file=sys.stderr)
            return 2
        out = svc.merge_candidate(args.table)
        print(f"Merged legacy candidate → v{out['version_after']}")
        return 0
    return 0 if result.valid else 1



def _run_retention(args, store: ContractStore):
    """`redibis retention show|set|clear <table>` — table-level TTL."""
    from redibis.contracts.retention import format_retention, NA

    if args.retention_action == "show":
        r = store.get_retention(args.table)
        if not r.get("set"):
            active = store.get_active(args.table)
            if active is None:
                print(f"No active contract for {args.table} (retention: {NA})",
                      file=sys.stderr)
                return 1
        line = NA if r["value"] == NA else format_retention({"slaProperties": [{
            "property": "retention", "value": r["value"], "unit": r["unit"],
            "driver": r.get("driver"), "element": r.get("element")}]})
        print(f"{args.table} retention: {line}"
              + ("" if r.get("set") else "  (default — not declared in contract)"))
        return 0

    if args.retention_action == "clear":
        try:
            result = store.set_retention(args.table, value=NA, run_id="retention-clear")
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 1
        print(f"{args.table} retention set to {NA} → v{result.version_after}")
        return 0

    # set
    value = args.value if args.value is not None else NA
    try:
        result = store.set_retention(
            args.table, value=value, unit=args.unit or NA,
            driver=args.driver, element=args.element,
        )
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    print(f"{args.table} retention → {value} {args.unit or ''}".rstrip()
          + f"  (v{result.version_after}, {'new' if result.is_new else 'updated'})")
    return 0


def _run_explicit_merge(args, store: ContractStore):
    merged = merge_odcs_contracts([store.get_active(args.table)], workflow="manual")
    print(json.dumps(merged, indent=2, default=str))
    return 0


def _run_show(args, store: ContractStore):
    contract = store.get_active(args.table)
    if contract is None:
        print(f"No active contract for {args.table}", file=sys.stderr)
        return 1
    print(yaml.safe_dump(contract, default_flow_style=False, allow_unicode=True))
    return 0


def _run_history(args, store: ContractStore):
    history = store.get_history(args.table)
    if not history:
        print(f"No history for {args.table}", file=sys.stderr)
        return 1
    for entry in history:
        print(
            f"  {entry.timestamp}  {entry.workflow:10s}  "
            f"v{entry.version}  {entry.run_uuid[:8]}..."
        )
    return 0


def _run_list(args, store: ContractStore):
    tables = store.list_tables()
    if not tables:
        print("No tables in contract store.")
        return 0
    for t in tables:
        print(f"  {t}")
    return 0


def _rewrite_enrich_argv(argv: list[str]) -> list[str]:
    """Keep ``redibis enrich TABLE`` working while allowing reserved actions.

    ``enrich run TABLE`` / ``enrich export-context DIR`` become
    ``enrich --enrich-action …`` so argparse does not treat the action as a table.
    """
    actions = {
        "run", "export-context", "add-context", "list-context", "remove-context",
    }
    store_true = {
        "--multistep", "--dry-run", "--automerge", "--list-providers",
        "--external-masked-ack", "--bypass-rai", "--use-s3", "--debug",
        "--include-custom", "--json", "--help", "-h",
    }
    # Only the real subcommand slot counts: a table or value named "enrich"
    # elsewhere in argv must not trigger the rewrite.
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--debug" or tok.startswith("--log-format="):
            i += 1
            continue
        if tok == "--log-format":
            i += 2
            continue
        break
    if i >= len(argv) or argv[i] != "enrich":
        return argv
    rest = argv[i + 1 :]
    j = 0
    while j < len(rest):
        tok = rest[j]
        if tok in ("-h", "--help"):
            return argv
        if tok.startswith("-"):
            if tok in store_true or "=" in tok:
                j += 1
                continue
            if j + 1 < len(rest) and not rest[j + 1].startswith("-"):
                j += 2
                continue
            j += 1
            continue
        if tok in actions:
            return argv[: i + 1] + ["--enrich-action", tok] + rest[:j] + rest[j + 1 :]
        return argv
    return argv


def main(argv: Optional[list[str]] = None):
    import os

    from redibis.obs import setup_logging

    argv = list(sys.argv[1:] if argv is None else argv)
    from redibis.cli.evidence_cmd import maybe_dispatch_scan_nested

    nested_rc = maybe_dispatch_scan_nested(argv)
    if nested_rc is not None:
        return nested_rc

    parser = argparse.ArgumentParser(prog="redibis")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="enable DEBUG logging (sets REDIBIS_LOG_LEVEL=DEBUG)",
    )
    parser.add_argument(
        "--log-format",
        choices=["rich", "json", "plain"],
        default=None,
        help="log output format (default: rich on TTY, json otherwise)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    def _common(p):
        p.add_argument(
            "--s3-endpoint",
            help="S3/MinIO endpoint URL (credentials: S3_ACCESS_KEY / AWS_ACCESS_KEY_ID env)",
        )
        p.add_argument("--s3-runs-bucket", default="pii-reports")
        p.add_argument("--s3-contracts-bucket", default="active-contracts")
        p.add_argument("--s3-pii-runs-bucket", default="pii-contracts")
        p.add_argument("--s3-quality-runs-bucket", default="quality-contracts")
        p.add_argument(
            "--use-s3", action="store_true",
            help="use S3 storage (default: local under --output-dir/_dev_storage)",
        )
        p.add_argument("--output-dir", default="./reports")

    def _scan_flags(p, *, default_mode: str = "all"):
        p.add_argument("file", help="path to a CSV/Parquet file")
        p.add_argument("table", nargs="?", help="schema.table (defaults from filename)")
        p.add_argument(
            "--mode", default=default_mode,
            help="scan scope: all | profile | pii | quality | comma list (e.g. pii,quality)",
        )
        p.add_argument("--automerge", choices=["none", "pii", "quality", "both"],
                        default="none", help="auto-merge the run into the active contract")
        p.add_argument("--equation",
                        choices=["strict", "balanced", "lenient", "independent"],
                        default="independent")
        p.add_argument("--pii-engines", choices=["regex", "gliner", "ner", "llm", "both"],
                        default="both")
        p.add_argument(
            "--ner-model", default="",
            help="local path to NER weights directory (or set REDIBIS_NER_MODEL)",
        )
        p.add_argument(
            "--gliner-model", default="",
            help="deprecated alias for --ner-model",
        )
        p.add_argument(
            "--ner-labels", default="",
            help="comma-separated GLiNER entity labels for this scan (overrides config/manifest)",
        )
        p.add_argument("--no-ge-docs", action="store_true")
        p.add_argument("--no-validate", action="store_true")
        p.add_argument("--session-id",
                        help="reuse a fixed session UUID (default: new session per scan)")
        p.add_argument("--scan-output-dir",
                        help="session root directory (default: --output-dir)")
        p.add_argument("--config", help="YAML config file (RedibisConfig)")
        p.add_argument("--profiler-engine", choices=["great_expectations", "open_metadata"],
                        help="override profiling.engine from config")
        p.add_argument(
            "--enable-metadata", action="store_true",
            help="enable Tier A catalog metadata (requires source.engine in config)",
        )
        p.add_argument(
            "--enable-pushdown", action="store_true",
            help="enable Tier B pushdown SQL aggregates for catalog gaps",
        )
        p.add_argument(
            "--enable-memory", action="store_true",
            help="enable column memory learning loop on this scan",
        )
        p.add_argument("--memory-domain", help="memory domain tag (ranking feature)")
        p.add_argument(
            "--memory-store", choices=["pgvector", "memory"],
            help="override memory.store from config",
        )
        p.add_argument(
            "--source-engine", choices=["none", "hive", "jdbc"],
            help="catalog engine for metadata/pushdown tiers",
        )
        p.add_argument(
            "--no-phonenumbers", action="store_true",
            help="disable libphonenumber for this run (regex/msisdn fallback)",
        )

    p_biz = sub.add_parser("import-business")
    _common(p_biz)
    p_biz.add_argument("--file")
    p_biz.add_argument("--dir")
    p_biz.add_argument("table", nargs="?", help="schema.table (or use --table-from-filename)")
    p_biz.add_argument("--table-from-filename", action="store_true")

    p_merge = sub.add_parser("merge", help="dump merged active contract JSON")
    _common(p_merge)
    p_merge.add_argument("table")

    p_show = sub.add_parser("show", help="print active contract YAML")
    _common(p_show)
    p_show.add_argument("table")

    p_hist = sub.add_parser("history", help="print contract audit trail")
    _common(p_hist)
    p_hist.add_argument("table")

    p_list = sub.add_parser("list", help="list tables with active contracts")
    _common(p_list)

    p_scan = sub.add_parser("scan", help="scan a file → run subcontracts (optionally automerge)")
    _common(p_scan)
    _scan_flags(p_scan)

    p_deep = sub.add_parser(
        "deep-scan",
        help="multi-producer deep scan → evidence artifacts + optional synthesis bundle",
    )
    _common(p_deep)
    p_deep.add_argument("file", help="path to a CSV/Parquet file")
    p_deep.add_argument("table", nargs="?", help="schema.table (defaults from filename)")
    p_deep.add_argument(
        "--producers",
        help="comma-separated producer ids (default: catalogue default_producers)",
    )
    p_deep.add_argument(
        "--bundle",
        action="store_true",
        help="build LLM-ready synthesis bundle (default: on)",
    )
    p_deep.add_argument("--no-bundle", action="store_true", help="skip synthesis bundle")
    p_deep.add_argument("--config", help="YAML config file (RedibisConfig)")

    for name, mode, help_text in (
        ("profile", "profile", "profile a file (no quality gatekeeper / PII)"),
        ("quality", "quality", "quality scan a file (profile + GE validation)"),
    ):
        p = sub.add_parser(name, help=help_text)
        _common(p)
        _scan_flags(p, default_mode=mode)

    p_config = sub.add_parser("config", help="configuration helpers")
    config_sub = p_config.add_subparsers(dest="config_action", required=True)
    p_config_dump = config_sub.add_parser("dump-default", help="print or write default redibis.yaml")
    p_config_dump.add_argument("-o", "--output", help="write to file instead of stdout")

    p_get = sub.add_parser("get", help="fetch run artifacts")
    get_sub = p_get.add_subparsers(dest="get_action", required=True)
    p_llm_logs = get_sub.add_parser(
        "llm-call-logs",
        help="bundle LLM enrichment evidence for a run or agent session",
    )
    p_llm_logs.add_argument("run_or_session_id", help="enrich run_id or agent session/run id")
    p_llm_logs.add_argument("--out", help="output directory (default: llm-call-logs-<id>)")
    p_llm_logs.add_argument("--zip", help="write a zip file instead of a directory")
    _common(p_llm_logs)

    p_runs = sub.add_parser("runs", help="list / merge / discard run subcontracts")
    runs_sub = p_runs.add_subparsers(dest="runs_action", required=True)

    def _runs_kind(p, *, run_required: bool = False):
        p.add_argument("table")
        p.add_argument("--kind", choices=["pii", "quality"], required=True)
        _common(p)
        if run_required:
            p.add_argument("--run", required=True, help="run_id to discard")
        else:
            p.add_argument("--run", help="run_id (merge picks latest if omitted)")

    p_runs_list = runs_sub.add_parser("list", help="list run subcontracts for a table")
    _runs_kind(p_runs_list)
    p_runs_merge = runs_sub.add_parser("merge", help="merge a run subcontract into active")
    _runs_kind(p_runs_merge)
    p_runs_merge.add_argument("--no-validate", action="store_true",
                              help="skip ODCS validation on merge")
    p_runs_discard = runs_sub.add_parser("discard", help="discard a run subcontract")
    _runs_kind(p_runs_discard, run_required=True)

    p_appr = sub.add_parser("approved",
                            help="review / merge a session's approved contract properties")
    appr_sub = p_appr.add_subparsers(dest="approved_action", required=True)

    def _approved_session(p, **extra):
        p.add_argument("--session", required=True, help="session id (or persisted dir)")
        _common(p)
        for flag, kwargs in extra.items():
            p.add_argument(flag, **kwargs)

    _approved_session(appr_sub.add_parser("list", help="list approved properties"),
                      **{"--json": {"action": "store_true", "help": "emit object JSON"}})
    _approved_session(appr_sub.add_parser("show", help="show one approved property"),
                      **{"--prop": {"required": True},
                         "--json": {"action": "store_true"}})
    _approved_session(appr_sub.add_parser("add-pii", help="add a PII column fragment"),
                      **{"--column": {"help": "target column"},
                         "--from-json": {"required": True, "dest": "from_json",
                                         "help": "detection row JSON path"}})
    _approved_session(appr_sub.add_parser("add-quality", help="add a quality rule fragment"),
                      **{"--column": {"help": "target column (omit for table-level)"},
                         "--from-json": {"required": True, "dest": "from_json",
                                         "help": "rule row JSON path"}})
    _approved_session(appr_sub.add_parser("remove", help="remove an approved property"),
                      **{"--prop": {"required": True}})
    _approved_session(appr_sub.add_parser("preview", help="preview assembled partials"))
    p_appr_merge = appr_sub.add_parser("merge", help="merge approved basket into contract")
    _approved_session(p_appr_merge, **{"--no-validate": {"action": "store_true"}})
    p_appr_clear = appr_sub.add_parser("clear", help="clear the approved basket")
    _approved_session(p_appr_clear, **{"--only-merged": {"action": "store_true", "dest": "only_merged",
                                                        "help": "clear only merged items"}})

    p_session = sub.add_parser("session", help="load flushed sessions from disk by run id")
    session_sub = p_session.add_subparsers(dest="session_action", required=True)
    p_session_load = session_sub.add_parser("load", help="load a flushed session into the live registry")
    p_session_load.add_argument("run_id", help="session folder name / run id")
    p_session_load.add_argument("--source", default="scan", choices=["scan", "agent"],
                                help="named session root (default: scan)")
    p_session_load.add_argument("--json", action="store_true", help="JSON output")
    p_session_list = session_sub.add_parser("list", help="list flushed sessions under a source")
    p_session_list.add_argument("--source", default="scan", choices=["scan", "agent"])
    p_session_list.add_argument("--json", action="store_true")
    p_session_sources = session_sub.add_parser("sources", help="list configured session roots")
    p_session_sources.add_argument("--json", action="store_true")

    p_rules = sub.add_parser("rules", help="list / export quality rules")
    rules_sub = p_rules.add_subparsers(dest="rules_action", required=True)
    p_rules_list = rules_sub.add_parser("list", help="list quality rules in active contract")
    p_rules_list.add_argument("table")
    _common(p_rules_list)
    p_rules_export = rules_sub.add_parser("export", help="export rules to GE / Soda / dbt")
    p_rules_export.add_argument("table")
    p_rules_export.add_argument("--target", choices=["ge", "sodacl", "dbt"], default="ge")
    _common(p_rules_export)

    p_ret = sub.add_parser("retention", help="show / set / clear table retention (TTL)")
    ret_sub = p_ret.add_subparsers(dest="retention_action", required=True)
    for action in ("show", "clear"):
        p = ret_sub.add_parser(action, help=f"{action} retention for a table")
        p.add_argument("table")
        _common(p)
    p_ret_set = ret_sub.add_parser("set", help="set retention TTL on a table")
    p_ret_set.add_argument("table")
    _common(p_ret_set)
    p_ret_set.add_argument("--value", help="retention duration (int) or NA")
    p_ret_set.add_argument("--unit", help="duration unit: d | w | m | y | h ...")
    p_ret_set.add_argument("--driver", help="why the limit exists (e.g. regulatory)")
    p_ret_set.add_argument("--element", help="column the TTL is measured from")

    p_contract = sub.add_parser("contract", help="contract lifecycle and views")
    contract_sub = p_contract.add_subparsers(dest="contract_action", required=True)

    def _contract_table(p):
        p.add_argument("table")
        _common(p)

    p_contract_purge = contract_sub.add_parser("purge", help="delete active contract + audit")
    _contract_table(p_contract_purge)
    p_contract_purge.add_argument("--keep-runs", action="store_true",
                                  help="retain run buckets for re-merge")
    for action, help_txt in (
        ("metadata", "operational telemetry (provenance, pii_summary)"),
        ("export-package", "spec + telemetry JSON bundle"),
        ("pii-view", "PII-only contract slice"),
        ("quality-view", "quality-only contract slice"),
        ("definitions-view", "business definitions slice"),
    ):
        p = contract_sub.add_parser(action, help=help_txt)
        _contract_table(p)
        p.add_argument("--json", action="store_true", help="JSON output")
    p_contract_strip = contract_sub.add_parser("strip-pii", help="demote column via decision overlay")
    _contract_table(p_contract_strip)
    p_contract_strip.add_argument("--column", required=True)
    p_contract_add = contract_sub.add_parser("add-pii", help="mark column as PII")
    _contract_table(p_contract_add)
    p_contract_add.add_argument("--column", required=True)
    p_contract_add.add_argument("--entity-type", required=True,
                              help="PII entity type, e.g. PHONE_NUMBER")
    p_contract_add.add_argument("--confidence", type=float, default=1.0)
    for action in ("quality-suppress", "quality-restore"):
        p = contract_sub.add_parser(action, help=f"{action.replace('-', ' ')} a quality rule")
        _contract_table(p)
        p.add_argument("--rule-id", required=True)
        p.add_argument("--column", help="column scope (optional)")
    p_contract_suppress_all = contract_sub.add_parser(
        "quality-suppress-all",
        help="remove all quality rules via decision overlay (no rescan)",
    )
    _contract_table(p_contract_suppress_all)
    p_contract_patch = contract_sub.add_parser("definitions-patch",
                                               help="patch business definitions from JSON")
    _contract_table(p_contract_patch)
    p_contract_patch.add_argument("--patch-file", required=True)

    from redibis.cli.synthesize_cmd import add_synthesize_parser
    add_synthesize_parser(contract_sub)

    p_enrich = sub.add_parser("enrich", help="LLM-enrich the full contract")
    _common(p_enrich)
    p_enrich.add_argument(
        "--enrich-action",
        default="run",
        choices=["run", "export-context", "add-context", "list-context", "remove-context"],
        help=argparse.SUPPRESS,
    )
    p_enrich.add_argument("table", nargs="?", help="schema.table to enrich")
    p_enrich.add_argument(
        "context_files",
        nargs="*",
        help=argparse.SUPPRESS,
    )
    p_enrich.add_argument(
        "--contract", "-f", dest="contract_file",
        help="ODCS contract YAML/JSON file (use instead of the active store contract)",
    )
    p_enrich.add_argument("--provider", default="demo",
                          help="provider name from llm_providers.json (demo=offline, or any LiteLLM backend)")
    p_enrich.add_argument("--providers-file",
                          help="path to llm_providers.json (else env REDIBIS_LLM_PROVIDERS / ./llm_providers.json / packaged default)")
    p_enrich.add_argument("--list-providers", action="store_true",
                          help="list configured providers and exit")
    p_enrich.add_argument("--model", default="",
                          help="override the provider's default model (bare name is auto-prefixed)")
    p_enrich.add_argument("--endpoint", help="override api_base (local vllm/ollama)")
    p_enrich.add_argument("--api-key")
    p_enrich.add_argument("--context", nargs="*",
                          help="context doc file paths (design docs, company/marketing info)")
    p_enrich.add_argument("--example-docs", nargs="*", dest="example_docs",
                          help="example doc file paths (uploaded few-shot examples)")
    p_enrich.add_argument("--examples", nargs="*", help="example contract tables (few-shot)")
    p_enrich.add_argument("--prompt", help="system prompt file path (overrides the default)")
    p_enrich.add_argument("--instructions",
                          help="extra prompt-engineering guidance (text or file path), appended to the system prompt")
    p_enrich.add_argument("--automerge", action="store_true",
                          help="validate then merge (fails closed on invalid)")
    p_enrich.add_argument(
        "--external-masked-ack", action="store_true",
        help="steward attests masked sample data uploaded for external LLM enrichment",
    )
    p_enrich.add_argument(
        "--bypass-rai", action="store_true",
        help="dev/open-source: skip RAI residency checks for this run (logged)",
    )
    p_enrich.add_argument(
        "--pack",
        help="enrichment pack folder or .zip (declarative prompts/context/examples)",
    )
    p_enrich.add_argument(
        "--dry-run",
        action="store_true",
        help="build prompts (and any reduction plan) without calling the LLM or writing",
    )
    p_enrich.add_argument(
        "--approve-context-reduction",
        metavar="PLAN_ID",
        help="exact context-reduction plan ID (crp_v1_...) required for non-interactive approval",
    )
    p_enrich.add_argument(
        "--config",
        help="path to redibis.yaml (else REDIBIS_CONFIG / defaults)",
    )
    p_enrich.add_argument(
        "--multistep",
        action="store_true",
        help="run YAML-defined LangGraph enrichment stages (requires redibis[agents] for LangGraph)",
    )
    p_enrich.add_argument(
        "--steps-file",
        help="enrichment workflow YAML (add/remove/reorder prebuilt stages)",
    )
    p_enrich.add_argument(
        "--scope",
        choices=["global", "table"],
        help="context overlay scope (add/list/remove-context)",
    )
    p_enrich.add_argument(
        "--mode",
        dest="context_mode",
        choices=["shared", "normal", "multistep"],
        default="shared",
        help="context overlay mode (add-context)",
    )
    p_enrich.add_argument(
        "--stage",
        dest="context_stage",
        help="multistep stage kind (add-context)",
    )
    p_enrich.add_argument(
        "--include-custom",
        action="store_true",
        help="include global custom overlays in export-context (off by default)",
    )
    p_enrich.add_argument(
        "--json",
        dest="context_json",
        action="store_true",
        help="JSON output for list-context",
    )
    p_enrich.add_argument(
        "--table",
        dest="context_table",
        help="table name for --scope table (add/list/remove-context)",
    )

    p_llm = sub.add_parser("llm", help="list / test / add / remove LLM providers; roles for capability routing")
    llm_sub = p_llm.add_subparsers(dest="llm_action", required=True)
    p_llm_list = llm_sub.add_parser("list", help="list registered providers")
    p_llm_list.add_argument(
        "--providers-file",
        help="path to llm_providers.json (else REDIBIS_LLM_PROVIDERS / ./llm_providers.json)",
    )
    p_llm_list.add_argument("--json", action="store_true", help="JSON output")
    p_llm_test = llm_sub.add_parser(
        "test",
        help="probe one provider (or --all); custom profiles use staged diagnostics",
    )
    p_llm_test.add_argument(
        "provider",
        nargs="?",
        help="provider name: ollama | vllm | sglang|slang | openai | claude | gemini | custom…",
    )
    p_llm_test.add_argument(
        "--all", action="store_true",
        help="test every registered provider except demo",
    )
    p_llm_test.add_argument(
        "--include-demo", action="store_true",
        help="with --all, also probe the offline demo provider",
    )
    p_llm_test.add_argument("--model", default="", help="override default model")
    p_llm_test.add_argument("--endpoint", help="override api_base (vllm / ollama / sglang)")
    p_llm_test.add_argument("--api-key", help="ephemeral API key (else api_key_env)")
    p_llm_test.add_argument(
        "--prompt",
        help="custom user prompt (default: Reply with exactly: OK)",
    )
    p_llm_test.add_argument("--timeout", type=float, default=20.0, help="seconds (default 20)")
    p_llm_test.add_argument(
        "--providers-file",
        help="path to llm_providers.json",
    )
    p_llm_test.add_argument("--json", action="store_true", help="JSON result")
    p_llm_test.add_argument(
        "--staged", action="store_true",
        help="force 3-stage test (models → completion → json_mode)",
    )
    p_llm_test.add_argument(
        "-v", "--verbose", action="store_true",
        help="print redacted provider debug log on failure/success",
    )

    p_llm_add = llm_sub.add_parser(
        "add",
        help="create a custom OpenAI-compatible (or raw LiteLLM) provider profile",
    )
    p_llm_add.add_argument("name", help="profile name (a-z, 0-9, _, -)")
    p_llm_add.add_argument("--api-base", help="OpenAI-compatible base URL (…/v1)")
    p_llm_add.add_argument("--model", help="served model id (must match GET /v1/models)")
    p_llm_add.add_argument("--litellm-model", help="full LiteLLM model string (for --raw)")
    p_llm_add.add_argument("--model-prefix", default="openai", help="LiteLLM prefix (default openai)")
    p_llm_add.add_argument("--api-key-env", help="env var name for the key (never store the key)")
    p_llm_add.add_argument("--description", default="", help="short description")
    p_llm_add.add_argument("--residency", default="local")
    p_llm_add.add_argument(
        "--param", action="append", default=[],
        help="free-form completion param k=v (repeatable), e.g. --param timeout=120",
    )
    p_llm_add.add_argument(
        "--no-json", action="store_true",
        help="disable response_format=json_object (SGLang builds that reject it)",
    )
    p_llm_add.add_argument(
        "--raw", action="store_true",
        help="raw LiteLLM model string mode (advanced)",
    )
    p_llm_add.add_argument(
        "--preset", choices=["sglang-qwen"],
        help="pre-fill SGLang + local Qwen defaults",
    )
    p_llm_add.add_argument(
        "--force", action="store_true",
        help="allow overriding a packaged provider name",
    )
    p_llm_add.add_argument("--providers-file", help="path to llm_providers.json")
    p_llm_add.add_argument("--json", action="store_true", help="JSON result")

    p_llm_rm = llm_sub.add_parser("remove", help="delete a custom provider profile")
    p_llm_rm.add_argument("name", help="custom profile name")
    p_llm_rm.add_argument("--providers-file", help="path to llm_providers.json")
    p_llm_rm.add_argument("--json", action="store_true", help="JSON result")

    p_llm_roles = llm_sub.add_parser("roles", help="list / show / test capability model roles")
    roles_sub = p_llm_roles.add_subparsers(dest="roles_action")
    p_roles_list = roles_sub.add_parser("list", help="list effective role bindings")
    p_roles_list.add_argument("--json", action="store_true")
    p_roles_show = roles_sub.add_parser("show", help="show one role binding")
    p_roles_show.add_argument("role", help="model role id (e.g. agent.planner)")
    p_roles_show.add_argument("--json", action="store_true")
    p_roles_test = roles_sub.add_parser("test", help="probe the provider bound to a role")
    p_roles_test.add_argument("role", help="model role id")
    p_roles_test.add_argument("--json", action="store_true")

    p_data = sub.add_parser("data", help="preview a data file")
    data_sub = p_data.add_subparsers(dest="data_action", required=True)
    p_data_preview = data_sub.add_parser("preview", help="print head rows of a file")
    p_data_preview.add_argument("file")
    p_data_preview.add_argument("--rows", type=int, default=10)

    p_mask = sub.add_parser("mask", help="de-identify a data file (plan/apply/auto/regex)")
    mask_sub = p_mask.add_subparsers(dest="mask_action", required=True)

    for action, help_txt in (
        ("plan", "write a masking plan YAML"),
        ("apply", "apply a saved plan to a file"),
        ("auto", "PII-suggest plan and apply in one step"),
    ):
        p_ma = mask_sub.add_parser(action, help=help_txt)
        p_ma.add_argument("file")
        p_ma.add_argument("--table", help="schema.table (defaults from filename)")
        p_ma.add_argument("--plan", help="masking plan YAML (apply)")
        p_ma.add_argument("--out", help="output path (plan.yaml or data_safe.csv)")
        p_ma.add_argument("--from-pii", action="store_true",
                          help="run PII detection to auto-suggest the plan (plan/auto)")
        p_ma.add_argument(
            "--locale",
            choices=["default", "ar", "en", "mixed"],
            default=None,
            help="default faker locale (config fallback when omitted)",
        )
        p_ma.add_argument(
            "--column-faker",
            action="append",
            metavar="COLUMN=MODE",
            help="per-column faker override: MODE is faker_arabic, faker_english, or default",
        )
        p_ma.add_argument("--seed", help="per-run seed (deterministic transforms)")
        p_ma.add_argument("--format", choices=["csv", "parquet"], default="csv")

    p_regex = mask_sub.add_parser("regex", help="regex pattern library (fake kind=regex)")
    p_caps = mask_sub.add_parser("capabilities", help="show masking crypto capabilities")
    p_caps.add_argument("--json", action="store_true", help="emit JSON")
    regex_sub = p_regex.add_subparsers(dest="regex_action", required=True)
    p_regex_list = regex_sub.add_parser("list", help="list registered regex patterns")
    p_regex_list.add_argument("--json", action="store_true", help="emit JSON")
    p_regex_test = regex_sub.add_parser("test", help="generate sample matching strings")
    p_regex_test.add_argument("-n", type=int, default=3, help="number of samples (max 20)")
    p_regex_test.add_argument("--pattern", help="inline regex pattern")
    p_regex_test.add_argument("--library", help="named pattern from the library (e.g. eg_mobile)")
    p_regex_test.add_argument("--deterministic", action="store_true",
                              help="use keyed PRNG (stable per seed)")
    p_regex_test.add_argument("--seed", help="seed when --deterministic")

    p_pii = sub.add_parser("pii", help="PII regex catalogue, NER inventory, and free-text scan")
    pii_sub = p_pii.add_subparsers(dest="pii_action", required=True)

    p_pii_regex = pii_sub.add_parser("regex", help="regex catalogue operations")
    pii_regex_sub = p_pii_regex.add_subparsers(dest="regex_action", required=True)
    p_pii_regex_export = pii_regex_sub.add_parser("export", help="export effective regex catalogue")
    p_pii_regex_export.add_argument("--format", choices=["json", "yaml", "csv"], default="json")
    p_pii_regex_export.add_argument("--active-only", action="store_true")
    p_pii_regex_export.add_argument("--out", help="output file (stdout if omitted)")
    p_pii_regex_export.add_argument("--config", help="apply pii.regex_overrides from YAML")
    p_pii_regex_list = pii_regex_sub.add_parser("list", help="list pattern names")
    p_pii_regex_list.add_argument("--json", action="store_true")
    p_pii_regex_list.add_argument("--active-only", action="store_true", default=True)
    p_pii_regex_list.add_argument("--config", help="apply pii.regex_overrides from YAML")

    p_pii_ner = pii_sub.add_parser("ner", help="NER model inventory")
    pii_ner_sub = p_pii_ner.add_subparsers(dest="ner_action", required=True)
    p_pii_ner_list = pii_ner_sub.add_parser("list", help="list discovered NER models")
    p_pii_ner_list.add_argument("--json", action="store_true")
    p_pii_ner_list.add_argument("--models-dir", help="override models root")
    p_pii_ner_list.add_argument("--config", help="read pii.models_dir from YAML")
    p_pii_ner_export = pii_ner_sub.add_parser("export", help="export NER model specs")
    p_pii_ner_export.add_argument("--out", default=".", help="output directory")
    p_pii_ner_export.add_argument("--bundle", help="also copy named model folder into --out")
    p_pii_ner_export.add_argument("--models-dir", help="override models root")
    p_pii_ner_export.add_argument("--config", help="read pii.models_dir from YAML")

    p_pii_text = pii_sub.add_parser("text", help="scan free text for PII spans")
    p_pii_text.add_argument("text", nargs="?", help="text to scan (or '-' for stdin)")
    p_pii_text.add_argument("--file", help="read text from file ('-' = stdin)")
    p_pii_text.add_argument("--language", default="en")
    p_pii_text.add_argument("--engines", default="both", help="regex|ner|both|phone or comma list")
    p_pii_text.add_argument("--min-score", type=float, default=0.35)
    p_pii_text.add_argument("--resolve", choices=["priority", "longest", "all"], default="priority")
    p_pii_text.add_argument("--entities", help="comma-separated entity types")
    p_pii_text.add_argument("--use-llm", action="store_true")
    p_pii_text.add_argument("--json", action="store_true")
    p_pii_text.add_argument(
        "--format",
        choices=["table", "json"],
        default=None,
        help="output format (default: table for TTY, or use --json)",
    )
    p_pii_text.add_argument("--redact", action="store_true", help="print text with [ENTITY] replacements")
    p_pii_text.add_argument("--no-text", action="store_true", help="omit matched substrings in JSON")
    p_pii_text.add_argument("--config", help="redibis.yaml")

    p_pii_deid = pii_sub.add_parser("deid", help="scan free text and apply a de-id policy")
    p_pii_deid.add_argument("text", nargs="?", help="text to de-identify (or '-' for stdin)")
    p_pii_deid.add_argument("--file", help="read text from file ('-' = stdin)")
    p_pii_deid.add_argument("--policy", help="named policy id (e.g. full-redact)")
    p_pii_deid.add_argument("--policy-file", help="YAML DeidPolicy file")
    p_pii_deid.add_argument("--default", help="ad-hoc default strategy (redact|mask|hash|fpe|fake)")
    p_pii_deid.add_argument(
        "--set", action="append", metavar="ENTITY=strategy",
        help="ad-hoc per-entity override (repeatable)",
    )
    p_pii_deid.add_argument("-o", "--out", help="write de-identified text to file")
    p_pii_deid.add_argument("--json", action="store_true", help="emit audit JSON on stderr")
    p_pii_deid.add_argument("--config", help="redibis.yaml")

    p_pii_cal = pii_sub.add_parser(
        "calibrate-nid",
        help="per-stage Egyptian National ID validation funnel on a CSV column",
    )
    p_pii_cal.add_argument("--input", required=True, help="CSV path")
    p_pii_cal.add_argument("--column", required=True, help="column name")
    p_pii_cal.add_argument("--sample", type=int, help="optional row sample size")
    p_pii_cal.add_argument("--config", help="redibis.yaml (reads pii.national_id_egypt)")

    p_pii_cal2 = pii_sub.add_parser(
        "calibrate",
        help="per-stage funnel for nid|imei|imsi|geo",
    )
    p_pii_cal2.add_argument(
        "--entity", required=True, choices=["nid", "imei", "imsi", "geo"],
    )
    p_pii_cal2.add_argument("--input", required=True, help="CSV path")
    p_pii_cal2.add_argument("--column", required=True, help="column name")
    p_pii_cal2.add_argument(
        "--partner-column", help="partner lat/lon column (geo only)",
    )
    p_pii_cal2.add_argument("--sample", type=int, help="optional row sample size")
    p_pii_cal2.add_argument("--config", help="redibis.yaml")

    p_models = sub.add_parser("models", help="manage BYOM NER model weights")
    models_sub = p_models.add_subparsers(dest="models_action", required=True)
    p_models_list = models_sub.add_parser("list", help="list models under REDIBIS_MODELS_DIR")
    p_models_list.add_argument("--json", action="store_true", help="emit JSON")
    p_models_list.add_argument(
        "--active", default="",
        help="path to mark as active (default: REDIBIS_NER_MODEL env)",
    )
    p_models_list.add_argument("--models-dir", help="override models root (before REDIBIS_MODELS_DIR env)")
    p_models_list.add_argument("--config", help="read pii.models_dir from this redibis.yaml")
    p_models_upload = models_sub.add_parser("upload", help="upload .zip / .tar.gz archive")
    p_models_upload.add_argument("archive", help="path to model archive")
    p_models_upload.add_argument("--name", help="destination folder name under models_dir")
    p_models_upload.add_argument("--models-dir", help="override models root (before REDIBIS_MODELS_DIR env)")
    p_models_upload.add_argument("--config", help="read pii.models_dir from this redibis.yaml")
    p_models_activate = models_sub.add_parser(
        "activate", help="select model for scans (print env hint or update --config YAML)",
    )
    p_models_activate.add_argument("name", help="model folder name under models_dir")
    p_models_activate.add_argument(
        "--config", help="read pii.models_dir and/or write pii.ner.model_path into redibis.yaml",
    )
    p_models_activate.add_argument("--models-dir", help="override models root when resolving name")
    p_models_delete = models_sub.add_parser("delete", help="remove model from REDIBIS_MODELS_DIR")
    p_models_delete.add_argument("name", help="model folder name under models_dir")
    p_models_delete.add_argument("--models-dir", help="override models root (before REDIBIS_MODELS_DIR env)")
    p_models_delete.add_argument("--config", help="read pii.models_dir from this redibis.yaml")
    p_models_delete.add_argument(
        "-y", "--yes", action="store_true", help="skip confirmation prompt",
    )

    p_catalog = sub.add_parser("catalog", help="push contracts to data-governance catalogs")
    catalog_sub = p_catalog.add_subparsers(dest="catalog_action", required=True)

    from redibis.cli.monitor_cmd import register_monitor_commands
    register_monitor_commands(sub, common_fn=_common)

    def _catalog_common(p):
        p.add_argument(
            "--config",
            help="redibis.yaml with catalog.backend (openmetadata|datahub|atlas); "
                 "or set REDIBIS_CONFIG env",
        )
        p.add_argument(
            "--backend",
            help="one-shot override of catalog.backend from config",
        )
        p.add_argument("--json", action="store_true", help="JSON output")

    p_catalog_back = catalog_sub.add_parser("backends", help="list catalog backends")
    _catalog_common(p_catalog_back)
    _common(p_catalog_back)

    p_catalog_push = catalog_sub.add_parser("push", help="push active contract(s) to catalog")
    _catalog_common(p_catalog_push)
    p_catalog_push.add_argument("table", nargs="?", help="schema.table")
    p_catalog_push.add_argument("--all", action="store_true", help="every table with an active contract")
    p_catalog_push.add_argument("--dry-run", action="store_true", help="print reconcile diff / payloads, no HTTP writes")
    p_catalog_push.add_argument(
        "--tables",
        help="required with --enforce; glob like 'telecom.*' limiting override targets",
    )
    p_catalog_push.add_argument(
        "--enforce",
        action="store_true",
        help="override human-owned facets (defaults to dry-run; pass --confirm to write)",
    )
    p_catalog_push.add_argument(
        "--clear-suppressions",
        action="store_true",
        help="with --enforce, also clear matching suppressions (separate irreversible act)",
    )
    p_catalog_push.add_argument(
        "--reason",
        help="required with --enforce; lands in catalog audit trail",
    )
    p_catalog_push.add_argument(
        "--confirm",
        action="store_true",
        help="with --enforce, actually write (without this, enforce is dry-run only)",
    )
    p_catalog_push.add_argument(
        "--refresh-resolution",
        action="store_true",
        help="bypass entity-resolution cache and re-lookup the OpenMetadata table FQN",
    )
    p_catalog_push.add_argument(
        "--fail-fast", action="store_true",
        help="with --all, stop on first table error (default: continue)",
    )
    _common(p_catalog_push)

    p_catalog_push_file = catalog_sub.add_parser(
        "push-file",
        help="push one ODCS contract file (yaml/json) to catalog; table from physicalName",
    )
    _catalog_common(p_catalog_push_file)
    p_catalog_push_file.add_argument("file", help="path to contract .yaml / .yml / .json")
    p_catalog_push_file.add_argument("--dry-run", action="store_true", help="preview payloads, no HTTP")
    _common(p_catalog_push_file)

    p_catalog_push_batch = catalog_sub.add_parser(
        "push-batch",
        help="push every contract file in a directory (scan/export folder) to catalog",
    )
    _catalog_common(p_catalog_push_batch)
    p_catalog_push_batch.add_argument(
        "path",
        help="directory of ODCS contracts (or a single file); e.g. reports/_dev_storage/active-contracts",
    )
    p_catalog_push_batch.add_argument(
        "--glob", help="filename pattern (default: *.yaml, *.yml, *.json)",
    )
    p_catalog_push_batch.add_argument(
        "-r", "--recursive", action="store_true",
        help="search subdirectories (use for scan/reports trees)",
    )
    p_catalog_push_batch.add_argument(
        "--fail-fast", action="store_true", help="stop on first file error",
    )
    p_catalog_push_batch.add_argument("--dry-run", action="store_true", help="preview payloads, no HTTP")
    _common(p_catalog_push_batch)

    p_catalog_push_scan = catalog_sub.add_parser(
        "push-scan",
        help="push active contracts for tables discovered in a scan-output folder",
    )
    _catalog_common(p_catalog_push_scan)
    p_catalog_push_scan.add_argument(
        "scan_dir", help="scan output directory containing session folders",
    )
    p_catalog_push_scan.add_argument(
        "--database", default="", help="only tables under this database/schema prefix",
    )
    p_catalog_push_scan.add_argument(
        "--include", nargs="*", default=None,
        help="include table globs (e.g. telecom.*)",
    )
    p_catalog_push_scan.add_argument(
        "--exclude", nargs="*", default=None,
        help="exclude table globs",
    )
    p_catalog_push_scan.add_argument(
        "--tables-file", help="file with one schema.table (or glob) per line",
    )
    p_catalog_push_scan.add_argument(
        "--resume",
        help="resume a prior run_id or path to catalog_push_runs/<id>/manifest.json",
    )
    p_catalog_push_scan.add_argument(
        "--no-retry-failed", action="store_true",
        help="on --resume, do not retry previously failed tables",
    )
    p_catalog_push_scan.add_argument(
        "--skip-in-sync", action="store_true",
        help="skip tables whose catalog telemetry already matches active version",
    )
    p_catalog_push_scan.add_argument(
        "--fail-fast", action="store_true", help="stop on first table error",
    )
    p_catalog_push_scan.add_argument("--dry-run", action="store_true", help="preview payloads, no HTTP")
    _common(p_catalog_push_scan)

    p_catalog_status = catalog_sub.add_parser("status", help="last catalog push vs contract version")
    _catalog_common(p_catalog_status)
    p_catalog_status.add_argument("table", help="schema.table")
    _common(p_catalog_status)

    p_catalog_open = catalog_sub.add_parser("open", help="print OpenMetadata UI URL for a table")
    _catalog_common(p_catalog_open)
    p_catalog_open.add_argument("table", help="schema.table")
    _common(p_catalog_open)

    p_catalog_feedback = catalog_sub.add_parser(
        "feedback",
        help="catalog feedback (human veto persistence from OM change events)",
    )
    feedback_sub = p_catalog_feedback.add_subparsers(dest="feedback_action", required=True)
    p_feedback_sync = feedback_sub.add_parser(
        "sync",
        help="poll OM change events → suppressions",
    )
    _catalog_common(p_feedback_sync)
    _common(p_feedback_sync)
    # Removed alias — kept so argparse accepts the name and the handler prints a pointer.
    p_catalog_sync_feedback = catalog_sub.add_parser(
        "sync-feedback",
        help=argparse.SUPPRESS,
    )
    _catalog_common(p_catalog_sync_feedback)
    _common(p_catalog_sync_feedback)

    p_catalog_ledger = catalog_sub.add_parser(
        "ledger", help="show projection ledger for a table",
    )
    ledger_sub = p_catalog_ledger.add_subparsers(dest="ledger_action")
    p_ledger_show = ledger_sub.add_parser("show", help="list ledger entries")
    _catalog_common(p_ledger_show)
    p_ledger_show.add_argument("table", nargs="?", help="schema.table")
    p_ledger_show.add_argument(
        "--tables",
        help="optional glob like 'telecom.*' to list ledger across matching tables",
    )
    _common(p_ledger_show)
    # Allow `redibis catalog ledger TABLE` as shorthand for show
    _catalog_common(p_catalog_ledger)
    p_catalog_ledger.add_argument("table", nargs="?", help="schema.table (shorthand for show)")
    p_catalog_ledger.add_argument(
        "--tables",
        help="optional glob like 'telecom.*' to list ledger across matching tables",
    )
    _common(p_catalog_ledger)

    p_catalog_supp = catalog_sub.add_parser(
        "suppression", help="list/add/remove catalog suppressions",
    )
    supp_sub = p_catalog_supp.add_subparsers(dest="suppression_action", required=True)
    p_supp_list = supp_sub.add_parser("list", help="list suppressions for a table")
    _catalog_common(p_supp_list)
    p_supp_list.add_argument("table", help="schema.table")
    _common(p_supp_list)
    p_supp_add = supp_sub.add_parser("add", help="add a suppression")
    _catalog_common(p_supp_add)
    p_supp_add.add_argument("table", help="schema.table")
    p_supp_add.add_argument("--column", default="", help="column path (empty = table-level)")
    p_supp_add.add_argument("--facet", required=True, help="facet enum value e.g. pii_tag")
    p_supp_add.add_argument("--value-key", required=True, dest="value_key", help="tag/term FQN")
    p_supp_add.add_argument("--asset-fqn", default="", dest="asset_fqn")
    p_supp_add.add_argument("--actor", default="")
    p_supp_add.add_argument("--reason", default="manual")
    _common(p_supp_add)
    p_supp_rm = supp_sub.add_parser("remove", help="remove a suppression")
    _catalog_common(p_supp_rm)
    p_supp_rm.add_argument("table", help="schema.table")
    p_supp_rm.add_argument("--column", default="")
    p_supp_rm.add_argument("--facet", required=True)
    p_supp_rm.add_argument("--value-key", required=True, dest="value_key")
    p_supp_rm.add_argument("--asset-fqn", default="", dest="asset_fqn")
    _common(p_supp_rm)
    # Removed plural alias
    p_catalog_supps = catalog_sub.add_parser(
        "suppressions",
        help=argparse.SUPPRESS,
    )
    _catalog_common(p_catalog_supps)
    _common(p_catalog_supps)

    p_catalog_delete = catalog_sub.add_parser(
        "delete",
        help="delete OpenMetadata table(s) pushed by redibis (requires --yes)",
    )
    _catalog_common(p_catalog_delete)
    p_catalog_delete.add_argument(
        "tables", nargs="*", metavar="TABLE",
        help="schema.table (maps to service.database.default_schema.table)",
    )
    p_catalog_delete.add_argument(
        "--fqn", action="append", default=[],
        help="raw OpenMetadata table FQN (repeatable)",
    )
    p_catalog_delete.add_argument(
        "--yes", action="store_true",
        help="execute deletes (without this flag, prints a dry-run plan only)",
    )
    p_catalog_delete.add_argument(
        "--soft", action="store_true",
        help="soft-delete in OM (default is hardDelete=true)",
    )
    _common(p_catalog_delete)

    p_catalog_wipe = catalog_sub.add_parser(
        "wipe",
        help="wipe the OpenMetadata database service tree (requires --yes)",
    )
    _catalog_common(p_catalog_wipe)
    p_catalog_wipe.add_argument(
        "--service",
        help="database service name to wipe (default: catalog.openmetadata.service_name)",
    )
    p_catalog_wipe.add_argument(
        "--yes", action="store_true",
        help="execute wipe (without this flag, prints a dry-run plan only)",
    )
    p_catalog_wipe.add_argument(
        "--soft", action="store_true",
        help="soft-delete in OM (default is hardDelete=true)",
    )
    p_catalog_wipe.add_argument(
        "--with-glossary", action="store_true",
        help="also delete glossary 'Redibis' (and its terms)",
    )
    p_catalog_wipe.add_argument(
        "--with-classifications", action="store_true",
        help="also delete classifications 'Redibis' and 'RedibisPolicy'",
    )
    _common(p_catalog_wipe)

    p_memory = sub.add_parser("memory", help="column memory store (enrichment learning loop)")
    memory_sub = p_memory.add_subparsers(dest="memory_action", required=True)

    def _memory_common(p):
        p.add_argument(
            "--config",
            help="redibis.yaml with memory block; or set REDIBIS_CONFIG env",
        )
        p.add_argument("--json", action="store_true", help="JSON output")

    p_memory_init = memory_sub.add_parser("init-db", help="create pgvector schema / verify store")
    _memory_common(p_memory_init)

    p_memory_status = memory_sub.add_parser("status", help="show memory configuration and row count")
    _memory_common(p_memory_status)

    p_memory_list = memory_sub.add_parser(
        "list", help="list all stored column fingerprints with steward approval evidence",
    )
    _memory_common(p_memory_list)
    p_memory_list.add_argument("--table", help="filter by schema.table seen in any decision")
    p_memory_list.add_argument(
        "--workflow",
        help="filter by provenance workflow (approved_merge, pii_decision, definitions_patch, …)",
    )
    p_memory_list.add_argument("--limit", type=int, default=500, help="max rows (default 500)")
    _common(p_memory_list)

    p_memory_search = memory_sub.add_parser(
        "search", help="search similar past steward reviews for a contract table",
    )
    _memory_common(p_memory_search)
    p_memory_search.add_argument("table", help="schema.table with an active contract")
    p_memory_search.add_argument("--column", help="limit search to one column")
    _common(p_memory_search)

    from redibis.cli.golden_cmd import register_golden_commands
    register_golden_commands(sub)

    from redibis.cli.behavior_cmd import register_behavior_commands
    register_behavior_commands(sub)

    from redibis.cli.pack_cmd import register_pack_commands
    register_pack_commands(sub)

    from redibis.cli.evidence_cmd import register_context_commands
    register_context_commands(sub)

    from redibis.cli.verdict_cmd import register_verdict_commands
    register_verdict_commands(sub)

    from redibis.cli.rdbpack_cmd import register_rdbpack_commands
    register_rdbpack_commands(sub)

    from redibis.cli.dataset_cmd import register_dataset_commands
    register_dataset_commands(sub)

    p_classify = sub.add_parser("classify", help="multi-domain classification policy engine")
    classify_sub = p_classify.add_subparsers(dest="classification_action", required=True)

    def _classify_common(p):
        p.add_argument("--config", help="redibis.yaml; or set REDIBIS_CONFIG env")
        p.add_argument("--json", action="store_true", help="JSON output")
        p.add_argument("--policy", default="telecom", help="builtin policy pack name")
        p.add_argument("--policy-file", help="path to custom policy pack YAML")

    p_classify_packs = classify_sub.add_parser("packs", help="list builtin policy packs")
    _classify_common(p_classify_packs)

    p_classify_desc = classify_sub.add_parser("describe-pack", help="show policy pack taxonomy")
    _classify_common(p_classify_desc)

    p_classify_run = classify_sub.add_parser("classify", help="classify a contract file")
    _classify_common(p_classify_run)
    p_classify_run.add_argument("file", help="ODCS contract .yaml/.json")
    p_classify_run.add_argument("--table", help="override schema.table")
    p_classify_run.add_argument("--column", help="classify one column only")
    p_classify_run.add_argument("--jurisdiction", help="steward jurisdiction signal (EU/IN/US)")
    p_classify_run.add_argument("--atlas-preview", action="store_true", help="include Atlas atomic payload")
    _common(p_classify_run)

    p_classify_active = classify_sub.add_parser(
        "classify-active", help="classify an active contract from the store",
    )
    _classify_common(p_classify_active)
    p_classify_active.add_argument("table", help="schema.table")
    p_classify_active.add_argument("--jurisdiction", help="steward jurisdiction signal")
    _common(p_classify_active)

    p_agents = sub.add_parser("agents", help="agentic pipeline board (export-only v1)")
    agents_sub = p_agents.add_subparsers(dest="agents_action", required=True)

    def _agents_common(p):
        p.add_argument("--json", action="store_true", help="JSON output")

    p_agents_nodes = agents_sub.add_parser("nodes", help="list pipeline node registry")
    _agents_common(p_agents_nodes)

    p_agents_compile = agents_sub.add_parser("compile", help="compile pipeline → prompt-plan")
    _agents_common(p_agents_compile)
    p_agents_compile.add_argument("--pipeline-file", help="pipeline spec YAML/JSON")
    p_agents_compile.add_argument("--edits-file", help="manual section edits YAML")
    p_agents_compile.add_argument("--show-diff", action="store_true", help="show diff after edits merge")
    p_agents_compile.add_argument("-o", "--output", help="write prompt-plan text to file")

    p_agents_example = agents_sub.add_parser("example", help="print example pipeline + plan")
    _agents_common(p_agents_example)

    def _agents_run_common(p):
        _agents_common(p)
        p.add_argument("--config", help="redibis.yaml; or set REDIBIS_CONFIG env")
        p.add_argument("--pipeline-file", help="pipeline spec YAML/JSON")
        p.add_argument("--runs-dir", help="override agents.runs_dir")
        p.add_argument("--tables", help="comma-separated schema.table list")
        p.add_argument("--database", help="filter contract store tables by database/schema")
        p.add_argument("--dry-run", action="store_true", help="catalog push dry-run only")
        p.add_argument("--sample-dir", help="directory of per-table sample CSVs")
        p.add_argument("--sample-paths", help="table=path mappings for scan steps")

    p_agents_run = agents_sub.add_parser("run", help="execute batch pipeline over tables")
    _agents_run_common(p_agents_run)
    _common(p_agents_run)

    p_agents_exec = agents_sub.add_parser(
        "execute", help="execute pipeline for one table (in-app Phase 5)",
    )
    _agents_run_common(p_agents_exec)
    p_agents_exec.add_argument("table", help="schema.table")
    p_agents_exec.add_argument("--sample", help="path to sample CSV/Parquet for scan steps")
    _common(p_agents_exec)

    p_agents_runs = agents_sub.add_parser("runs", help="list agent batch runs")
    _agents_common(p_agents_runs)
    p_agents_runs.add_argument("--config", help="redibis.yaml")
    p_agents_runs.add_argument("--runs-dir", help="override agents.runs_dir")
    p_agents_runs.add_argument("--limit", type=int, default=50)

    p_agents_status = agents_sub.add_parser("status", help="show agent run status + ledger")
    _agents_common(p_agents_status)
    p_agents_status.add_argument("run_id")
    p_agents_status.add_argument("--config", help="redibis.yaml")
    p_agents_status.add_argument("--runs-dir", help="override agents.runs_dir")

    p_agents_trace = agents_sub.add_parser("trace", help="DAG trace (React Flow JSON)")
    _agents_common(p_agents_trace)
    p_agents_trace.add_argument("run_id")
    p_agents_trace.add_argument("--config", help="redibis.yaml")
    p_agents_trace.add_argument("--runs-dir", help="override agents.runs_dir")

    p_agents_cancel = agents_sub.add_parser("cancel", help="cancel a running batch job")
    p_agents_cancel.add_argument("run_id")
    p_agents_cancel.add_argument("--reason", default="")
    p_agents_cancel.add_argument("--config", help="redibis.yaml")
    p_agents_cancel.add_argument("--runs-dir", help="override agents.runs_dir")

    p_agents_docgen = agents_sub.add_parser("docgen", help="auto-document a pipeline spec")
    _agents_common(p_agents_docgen)
    p_agents_docgen.add_argument("--pipeline-file", help="pipeline spec YAML/JSON")
    p_agents_docgen.add_argument("-o", "--output", help="write markdown to file")

    p_agents_plugins = agents_sub.add_parser("plugins", help="list entry-point agent tool plugins")
    _agents_common(p_agents_plugins)

    p_report = sub.add_parser(
        "report",
        help="PII reporting (requires commercial redibis-reports add-on)",
    )
    report_sub = p_report.add_subparsers(dest="report_action", required=True)
    p_rep_pii = report_sub.add_parser("pii", help="compute a PII report from scan outputs")
    p_rep_pii.add_argument("--root", default="./scan_output", help="local scan root (allowlisted)")
    p_rep_pii.add_argument("--source", choices=("local", "minio"), default="local")
    p_rep_pii.add_argument("--prefix", default="", help="MinIO key prefix when --source minio")
    p_rep_pii.add_argument("--schema", dest="schema_contains", default=None)
    p_rep_pii.add_argument("--table", dest="table_contains", default=None)
    p_rep_pii.add_argument("--min-confidence", type=float, default=0.0)
    p_rep_pii.add_argument("--max-confidence", type=float, default=1.0)
    p_rep_pii.add_argument("--regex", choices=("on", "off", "only"), default="on")
    p_rep_pii.add_argument("--ner", choices=("on", "off", "only"), default="on")
    p_rep_pii.add_argument("--llm", choices=("on", "off", "only"), default="on")
    p_rep_pii.add_argument("--phone", choices=("on", "off", "only"), default="on")
    p_rep_pii.add_argument("--runs", choices=("latest", "all"), default="latest")
    p_rep_pii.add_argument("--pii-only", action="store_true")
    p_rep_pii.add_argument("--overwrite-latest", action="store_true")
    p_rep_pii.add_argument("--out", dest="out_dir", default=None, help="override _reports dir (allowlisted)")
    p_rep_list = report_sub.add_parser("list", help="list report_run_ids")
    p_rep_list.add_argument("--root", default="./scan_output")
    p_rep_list.add_argument("--source", choices=("local", "minio"), default="local")
    p_rep_show = report_sub.add_parser("show", help="print report manifest / KPIs")
    p_rep_show.add_argument("report_run_id")
    p_rep_show.add_argument("--root", default="./scan_output")
    p_rep_show.add_argument("--source", choices=("local", "minio"), default="local")
    p_rep_prune = report_sub.add_parser("prune", help="keep newest N report versions (+ latest)")
    p_rep_prune.add_argument("--root", default="./scan_output")
    p_rep_prune.add_argument("--source", choices=("local", "minio"), default="local")
    p_rep_prune.add_argument("--keep", type=int, required=True)

    raw_argv = list(sys.argv[1:] if argv is None else argv)
    raw_argv = _rewrite_enrich_argv(raw_argv)
    args = parser.parse_args(raw_argv)

    if getattr(args, "debug", False):
        os.environ["REDIBIS_LOG_LEVEL"] = "DEBUG"

    from redibis.config import RedibisConfig
    from redibis.telemetry.init import init_otel

    rb_for_obs = None
    if getattr(args, "config", None):
        rb_for_obs = RedibisConfig.from_yaml(args.config)

    obs = rb_for_obs.observability if rb_for_obs is not None else RedibisConfig.default().observability
    setup_logging(
        level=os.environ.get("REDIBIS_LOG_LEVEL") or obs.log_level,
        fmt=getattr(args, "log_format", None) or obs.log_format or None,
        module_levels=obs.module_levels,
        decision_log=obs.decision_log,
    )
    init_otel(rb_for_obs or RedibisConfig.default())

    # These commands operate on local files only and never touch S3/contracts.
    if args.cmd == "report":
        return _run_report(args)
    if args.cmd == "data":
        return _run_data(args)
    if args.cmd == "mask":
        if args.mask_action == "regex":
            return _run_mask_regex(args)
        if args.mask_action == "capabilities":
            return _run_mask_capabilities(args)
        return _run_mask(args)
    if args.cmd == "pii":
        return _run_pii(args)
    if args.cmd == "llm":
        from redibis.cli.llm_cmd import run_llm
        return run_llm(args)
    if args.cmd == "pack":
        from redibis.cli.pack_cmd import run_pack
        return run_pack(args)
    if args.cmd == "context":
        from redibis.cli.evidence_cmd import run_context
        return run_context(args)
    if args.cmd == "verdict":
        from redibis.cli.verdict_cmd import run_verdict_export, run_verdict_import, run_verdict_preview
        if args.verdict_action == "export":
            return run_verdict_export(args)
        if args.verdict_action == "preview":
            return run_verdict_preview(args)
        if args.verdict_action == "import":
            return run_verdict_import(args)
        print(f"unknown verdict action: {args.verdict_action}", file=sys.stderr)
        return 2
    if args.cmd == "rdbpack":
        from redibis.cli.rdbpack_cmd import run_rdbpack
        return run_rdbpack(args)
    if args.cmd == "dataset":
        from redibis.cli.dataset_cmd import run_dataset
        return run_dataset(args)
    if args.cmd == "models":
        from redibis.cli.models_cmd import run_models
        return run_models(args)
    if args.cmd == "session":
        from redibis.cli.tools import cmd_session
        return cmd_session(args)

    if args.cmd == "behavior":
        from redibis.cli.behavior_cmd import run_behavior
        return run_behavior(args)

    if args.cmd == "deep-scan":
        return _run_deep_scan(args)

    backend = _build_backend(args)
    store = _contract_store(args, backend)

    if args.cmd == "memory":
        from redibis.cli.memory_cmd import run_memory
        return run_memory(args, store)

    if args.cmd in ("golden", "vector", "similar"):
        return args.func(args)

    if args.cmd == "catalog":
        from redibis.cli.catalog_cmd import run_catalog
        return run_catalog(args, store)

    if args.cmd in ("quality-monitor", "monitor"):
        from redibis.cli.monitor_cmd import run_monitor
        return run_monitor(
            args, store, backend,
            runs_bucket=getattr(args, "s3_runs_bucket", "pii-reports"),
        )

    if args.cmd == "classify":
        from redibis.cli.classification_cmd import run_classification
        return run_classification(args, store)

    if args.cmd == "agents":
        from redibis.cli.agents_cmd import run_agents
        return run_agents(args, store)

    if args.cmd == "import-business":
        return _run_business_import(args, store)
    if args.cmd == "merge":
        return _run_explicit_merge(args, store)
    if args.cmd == "show":
        return _run_show(args, store)
    if args.cmd == "history":
        return _run_history(args, store)
    if args.cmd == "list":
        return _run_list(args, store)
    if args.cmd == "scan":
        return _run_scan(args, store, backend)
    if args.cmd in ("profile", "quality"):
        return _run_scan(args, store, backend)
    if args.cmd == "config":
        if args.config_action == "dump-default":
            return _run_config_dump(args)
        return 2
    if args.cmd == "get":
        from redibis.cli.get_cmd import run_get
        return run_get(args, store, backend)
    if args.cmd == "runs":
        return _run_runs(args, store, backend)
    if args.cmd == "rules":
        return _run_rules(args, store)
    if args.cmd == "contract":
        if args.contract_action == "purge":
            return _run_purge(args, store, backend)
        if args.contract_action == "metadata":
            return _run_contract_metadata(args, store)
        if args.contract_action == "export-package":
            return _run_contract_export_package(args, store)
        if args.contract_action in ("strip-pii", "add-pii"):
            return _run_pii_decision(args, store)
        if args.contract_action in ("pii-view", "quality-view", "definitions-view"):
            return _run_contract_view(args, store)
        if args.contract_action in ("quality-suppress", "quality-restore", "quality-suppress-all"):
            return _run_quality_decision(args, store)
        if args.contract_action == "definitions-patch":
            return _run_definitions_patch(args, store)
        if args.contract_action == "synthesize":
            from redibis.cli.synthesize_cmd import run_synthesize
            return run_synthesize(args)
    if args.cmd == "retention":
        return _run_retention(args, store)
    if args.cmd == "enrich":
        return _run_enrich(args, store)
    if args.cmd == "approved":
        from redibis.cli.tools import cmd_approved
        return cmd_approved(args, store, backend)


if __name__ == "__main__":
    sys.exit(main() or 0)
