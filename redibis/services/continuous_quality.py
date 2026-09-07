"""Continuous quality monitoring orchestration (validate-only, no contract writes)."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

import pandas as pd
import yaml

from redibis.config import RedibisConfig
from redibis.quality.contract_validate import validate_contract_quality
from redibis.quality.rule_set import QualityRuleSet
from redibis.quality.schema import RuleSetRef, quality_run_from_validate_result, rule_set_from_contract
from redibis.quality.sinks.registry import get_quality_sink_class, resolve_sink_names
from redibis.quality.sinks.base import QualitySinkOptions
from redibis.quality.validate_models import (
    BatchQualityValidateResult,
    ContractQualityValidateResult,
)
from redibis.services.source_sample import load_table_sample
from redibis.store.contract_store import ContractStore
from redibis.store.quality_decisions import effective_contract_quality
from redibis.store.run_output_writer import RunOutputWriter
from redibis.store.storage_backend import StorageBackend

log = logging.getLogger(__name__)

MONITOR_WORKFLOW = "monitor"
QUALITY_RESULTS_ARTIFACT = "quality_results.json"
QUALITY_RUN_ARTIFACT = "quality_run.json"
MONITOR_SUMMARY_ARTIFACT = "monitor_summary.json"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _expand_sample_path(sample_path: Optional[str], table: str) -> Optional[str]:
    """Expand ``{table}`` / ``{table_safe}`` in a batch ``--sample`` template.

    A literal path with no placeholders is used verbatim for every table
    (the pre-existing "one sample for the whole batch" behavior).
    """
    if not sample_path or "{" not in sample_path:
        return sample_path
    return sample_path.format(table=table, table_safe=table.replace(".", "_"))


@dataclass
class MonitorRunOptions:
    rows: int = 5000
    sample_path: Optional[str] = None
    include_schema_drift: bool = True
    generate_docs: bool = True
    publish_openmetadata: bool = True
    output_dir: Optional[str] = None
    run_id: Optional[str] = None
    spark: Any = None
    rules_file: Optional[str] = None


@dataclass
class ContinuousQualityService:
    """Validate approved contract rules and persist monitoring artifacts."""

    store: ContractStore
    backend: StorageBackend
    runs_bucket: str
    config: RedibisConfig = field(default_factory=RedibisConfig.default)

    def resolve_tables(
        self,
        *,
        tables: Optional[Sequence[str]] = None,
        database: Optional[str] = None,
        all_contracts: bool = False,
    ) -> list[str]:
        """Resolve which tables a batch run covers.

        Requires an explicit selector — ``tables``, ``database``, or
        ``all_contracts=True`` — so a bare call never silently monitors
        every contracted table.
        """
        if tables:
            return [str(t).strip() for t in tables if str(t).strip()]
        if not all_contracts and not database:
            return []
        listed = self.store.list_tables()
        if database:
            prefix = f"{database}."
            listed = [t for t in listed if t.startswith(prefix)]
        return sorted(listed)

    def load_sample(
        self,
        table: str,
        options: MonitorRunOptions,
    ) -> pd.DataFrame:
        if options.sample_path:
            return load_table_sample(table, file_path=options.sample_path, rows=options.rows)
        return load_table_sample(
            table,
            source_config=self.config.source,
            rows=options.rows,
            spark=options.spark,
        )

    def run_table(
        self,
        table: str,
        options: Optional[MonitorRunOptions] = None,
    ) -> ContractQualityValidateResult:
        opts = options or MonitorRunOptions()
        run_id = opts.run_id or uuid.uuid4().hex[:12]
        contract = self.store.get_active(table)
        if contract is None:
            return ContractQualityValidateResult(
                table=table,
                run_id=run_id,
                status="skipped",
                rules_total=0,
                rules_passed=0,
                rules_failed=0,
                error=f"No active contract for {table!r}",
                started_at=_utc_now_iso(),
                completed_at=_utc_now_iso(),
            )

        try:
            df = self.load_sample(table, opts)
        except Exception as exc:  # noqa: BLE001
            return ContractQualityValidateResult(
                table=table,
                run_id=run_id,
                status="error",
                rules_total=0,
                rules_passed=0,
                rules_failed=0,
                error=f"{type(exc).__name__}: {exc}",
                started_at=_utc_now_iso(),
                completed_at=_utc_now_iso(),
            )

        run_dir = None
        if opts.output_dir:
            safe = table.replace(".", "_")
            run_dir = str(Path(opts.output_dir) / safe / run_id)

        rule_set_override = None
        if opts.rules_file:
            rule_set_override = QualityRuleSet.from_dict(
                yaml.safe_load(Path(opts.rules_file).read_text(encoding="utf-8"))
            )

        # Suppressed rules must never run and approved manual rules always
        # must — apply the same overlay ContractStore.upsert() reconciles at
        # write time, in memory, without touching the stored contract. An
        # explicit --rules-file override bypasses the overlay entirely (the
        # caller supplied the exact rule set to execute).
        decisions = self.store.quality_decisions.get(table) if rule_set_override is None else None
        effective_contract = effective_contract_quality(contract, decisions)

        result, qa, raw_ge = validate_contract_quality(
            df,
            effective_contract,
            table=table,
            run_id=run_id,
            include_schema_drift=opts.include_schema_drift,
            generate_docs=opts.generate_docs,
            run_dir=run_dir,
            rule_set_override=rule_set_override,
        )

        rule_set_ref = RuleSetRef.from_rule_set(rule_set_from_contract(effective_contract, table=table))
        canonical_run = quality_run_from_validate_result(
            result,
            table=table,
            rule_set_ref=rule_set_ref,
            contract=effective_contract,
            batch={"sample_rows": opts.rows, "sample_path": opts.sample_path or ""},
        )

        artifact_keys = self._persist_artifacts(
            table, result, qa, raw_ge=raw_ge, run_dir=run_dir, generate_docs=opts.generate_docs,
            canonical_run=canonical_run,
        )
        result.artifact_keys = artifact_keys
        canonical_run.artifacts = dict(artifact_keys)

        if opts.publish_openmetadata:
            result.sink_telemetry = self._publish_results(
                table, effective_contract, canonical_run, run_id=run_id,
                artifact_ref=artifact_keys.get(QUALITY_RESULTS_ARTIFACT, ""),
            )

        return result

    def run_batch(
        self,
        tables: Sequence[str],
        options: Optional[MonitorRunOptions] = None,
    ) -> BatchQualityValidateResult:
        opts = options or MonitorRunOptions()
        batch_id = opts.run_id or uuid.uuid4().hex[:12]
        per_table: list[ContractQualityValidateResult] = []
        passed = failed = skipped = 0
        for table in tables:
            table_opts = MonitorRunOptions(
                rows=opts.rows,
                sample_path=_expand_sample_path(opts.sample_path, table),
                include_schema_drift=opts.include_schema_drift,
                generate_docs=opts.generate_docs,
                publish_openmetadata=opts.publish_openmetadata,
                output_dir=opts.output_dir,
                run_id=f"{batch_id}_{table.replace('.', '_')}",
                spark=opts.spark,
            )
            res = self.run_table(table, table_opts)
            per_table.append(res)
            if res.status == "success":
                passed += 1
            elif res.status == "skipped":
                skipped += 1
            else:
                failed += 1
        return BatchQualityValidateResult(
            run_id=batch_id,
            tables_total=len(tables),
            tables_passed=passed,
            tables_failed=failed,
            tables_skipped=skipped,
            per_table=per_table,
        )

    def _persist_artifacts(
        self,
        table: str,
        result: ContractQualityValidateResult,
        qa: Any,
        *,
        raw_ge: Any = None,
        run_dir: Optional[str],
        generate_docs: bool,
        canonical_run: Any = None,
    ) -> dict[str, str]:
        keys: dict[str, str] = {}
        writer = RunOutputWriter(
            self.backend,
            bucket=self.runs_bucket,
            workflow=MONITOR_WORKFLOW,
            table=table,
            run_id=result.run_id,
        )

        payload = result.to_dict()
        keys[QUALITY_RESULTS_ARTIFACT] = writer.write(QUALITY_RESULTS_ARTIFACT, payload)
        if canonical_run is not None:
            keys[QUALITY_RUN_ARTIFACT] = writer.write(QUALITY_RUN_ARTIFACT, canonical_run.to_dict())
        summary = {
            "table": table,
            "run_id": result.run_id,
            "status": result.status,
            "rules_total": result.rules_total,
            "rules_passed": result.rules_passed,
            "rules_failed": result.rules_failed,
            "schema_drift": (result.schema_drift.to_dict() if result.schema_drift else None),
            "completed_at": result.completed_at,
        }
        keys[MONITOR_SUMMARY_ARTIFACT] = writer.write(MONITOR_SUMMARY_ARTIFACT, summary)

        if run_dir:
            rd = Path(run_dir)
            rd.mkdir(parents=True, exist_ok=True)
            qr_path = rd / f"quality-report-{result.run_id}.html"
            try:
                report_data = qa._extract_report_data(raw_ge) if raw_ge is not None else {"results": []}
                qa.export_quality_report(
                    report_data,
                    output_filename=str(qr_path),
                    report_title=f"Monitor — {table}",
                    open_browser=False,
                )
                keys["quality_report"] = writer.write_file(
                    f"quality-report-{result.run_id}.html", qr_path,
                )
            except Exception as exc:  # noqa: BLE001
                log.debug("quality report export skipped: %s", exc)

            if generate_docs and not qa.in_memory:
                ge_dir = rd / "ge_report"
                try:
                    qa.copy_data_docs_to(ge_dir)
                    for path in sorted(ge_dir.rglob("*")):
                        if path.is_file():
                            rel = path.relative_to(rd).as_posix()
                            keys[rel] = writer.write_file(rel, path)
                except Exception as exc:  # noqa: BLE001
                    log.debug("ge docs copy skipped: %s", exc)

            local_results = rd / QUALITY_RESULTS_ARTIFACT
            local_results.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

        from redibis.services.pipeline import (
            build_scan_coverage,
            write_scan_coverage,
        )

        columns = list({r.column for r in result.results if r.column})
        if result.schema_drift:
            columns = sorted(set(columns) | set(result.schema_drift.observed_columns))
        coverage = build_scan_coverage(
            scan_id=result.run_id,
            columns=columns,
            run_quality=True,
            quality_results=result.results,
            quality_error=result.error if result.status == "error" else None,
            empty_sample=result.rules_total == 0 and result.status != "skipped",
        )
        if coverage:
            write_scan_coverage(writer, coverage, table=table, scan_id=result.run_id)

        return keys

    def _publish_results(
        self,
        table: str,
        contract: dict,
        run: Any,
        *,
        run_id: str,
        artifact_ref: str,
    ) -> dict[str, Any]:
        """Dispatch ``run`` to every configured result sink.

        A sink failure is logged and recorded per-sink; it never fails the
        underlying validation (monitor is validate-only by design).
        """
        telemetry: dict[str, Any] = {}
        options = QualitySinkOptions(run_id=run_id, artifact_ref=artifact_ref)
        for name in resolve_sink_names(self.config):
            try:
                sink = get_quality_sink_class(name).from_config(self.config)
                telemetry[name] = sink.publish(table=table, contract=contract, run=run, options=options)
            except Exception as exc:  # noqa: BLE001
                log.warning("quality result sink %r failed for %s: %s", name, table, exc)
                telemetry[name] = {"published": False, "error": f"{type(exc).__name__}: {exc}"}
        return telemetry


def load_latest_quality_results(
    backend: StorageBackend,
    bucket: str,
    table: str,
    *,
    workflow: str = MONITOR_WORKFLOW,
) -> Optional[dict[str, Any]]:
    """Load ``quality_results.json`` from the most recently *completed* monitor run.

    Run ids are random (``uuid4().hex[:12]``), not time-ordered, so "latest"
    is resolved from each run's recorded ``completed_at`` timestamp (via the
    small ``monitor_summary.json`` sidecar) rather than the run id itself.
    """
    table_safe = table.replace(".", "_")
    prefix = f"{workflow}/{table_safe}/"
    run_prefixes: set[str] = set()
    try:
        keys = backend.list_keys(bucket, prefix=prefix)
    except Exception:  # noqa: BLE001
        return None
    for key in keys:
        parts = key[len(prefix):].split("/")
        if parts and parts[0]:
            run_prefixes.add(parts[0])
    if not run_prefixes:
        return None

    best_run: Optional[str] = None
    best_ts = ""
    for run_id in run_prefixes:
        ts = ""
        summary_key = f"{prefix}{run_id}/{MONITOR_SUMMARY_ARTIFACT}"
        try:
            if backend.exists(bucket, summary_key):
                summary = backend.get_json(bucket, summary_key) or {}
                ts = str(summary.get("completed_at") or "")
        except Exception:  # noqa: BLE001
            ts = ""
        if best_run is None or ts > best_ts or (ts == best_ts and run_id > best_run):
            best_run, best_ts = run_id, ts

    artifact = f"{prefix}{best_run}/{QUALITY_RESULTS_ARTIFACT}"
    try:
        if not backend.exists(bucket, artifact):
            return None
        return backend.get_json(bucket, artifact)
    except Exception:  # noqa: BLE001
        return None


def export_monitoring_bundle(
    table: str,
    contract: dict,
    *,
    rules: Optional[list[dict]] = None,
    dropped_rule_ids: Optional[list[str]] = None,
    schedule: str = "0 6 * * *",
    output_dir: str | Path = ".",
    engine: str = "spark",
) -> Path:
    """Write a per-table monitoring package directory."""
    from redibis.quality.monitoring_package import build_monitoring_package

    return build_monitoring_package(
        table=table,
        contract=contract,
        rules=rules,
        dropped_rule_ids=dropped_rule_ids or [],
        schedule=schedule,
        output_dir=Path(output_dir),
        engine=engine,
    )
