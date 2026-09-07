"""Scan/discovery step executors for the web session layer."""
from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

import pandas as pd
import yaml

from redibis.models import PIIDetection
from redibis.quality.gatekeeper import QualityGatekeeper
from redibis.services.scan_service import ScanConfig
from redibis.services.session.config import GlobalConfig, build_subcontract_store, _build_scan_config, _effective_ner_model_path
from redibis.services.session.state import (
    RunRecord,
    ScanSession,
    _load_dataframe,
    split_table,
)
from redibis.store.contract_store import ContractStore
from redibis.store.run_merger import RunMerger
from redibis.store.storage_backend import StorageBackend

log = logging.getLogger(__name__)

def _session_dir(session: ScanSession) -> Path:
    return Path(session.data_path).parent


def _session_run_writer(session: ScanSession, backend: Optional[StorageBackend], run_id: str):
    if backend is None or not run_id:
        return None
    from redibis.store.run_output_writer import RunOutputWriter

    return RunOutputWriter(
        backend=backend,
        bucket=os.getenv("S3_RUNS_BUCKET", "pii-reports"),
        workflow="scan",
        table=session.table_name,
        run_id=f"{session.session_id}/{run_id}",
    )


def _flush_session_evidence(
    session: ScanSession,
    run_result,
    scan_config: ScanConfig,
    run_art_dir: Path,
    df: Optional[pd.DataFrame],
    backend: Optional[StorageBackend],
) -> dict:
    from redibis.scan.report_bundle import ReportBundle

    if run_result is None:
        return {}
    run_result.session_id = session.session_id
    writer = _session_run_writer(session, backend, getattr(run_result, "run_id", "") or "")
    try:
        flushed = ReportBundle(run_result, scan_config).flush(
            run_art_dir, run_writer=writer, df=df,
        )
        session.artifacts.update(flushed)
        return flushed
    except Exception as flush_exc:
        session.add_log(f"Warning: evidence flush failed: {flush_exc}")
        return {}


def _filter_session_df(session: ScanSession, config: ScanConfig, df: pd.DataFrame) -> pd.DataFrame:
    if config.pii_columns:
        valid = [c for c in config.pii_columns if c in df.columns]
        if valid:
            return df[valid]
    return df


def _quality_fail_reason(r: dict) -> str:
    if r.get("success"):
        return ""
    parts: list[str] = []
    uc = r.get("unexpected_count")
    if uc:
        parts.append(f"{uc} unexpected value(s)")
    samples = r.get("partial_unexpected") or []
    if samples:
        parts.append("samples: " + ", ".join(str(x) for x in samples[:3]))
    obs = r.get("observed_value")
    if obs is not None:
        parts.append(f"observed: {obs}")
    return "; ".join(parts) or "check failed"


def _sync_quality_stats(
    session: ScanSession,
    run: RunRecord,
    qa: Optional[QualityGatekeeper],
    quality_results: Any = None,
) -> None:
    try:
        results: list[dict] = []
        if quality_results is not None and qa is not None:
            report = qa._extract_report_data(quality_results)
            results = report.get("results", [])
            stats = report.get("statistics", {})
            run.quality_total = int(
                stats.get("evaluated_expectations") or len(results) or 0
            )
            run.quality_passed = int(
                stats.get("successful_expectations")
                or sum(1 for r in results if r.get("success"))
            )
        else:
            exps = getattr(qa, "expectations", []) if qa else []
            run.quality_total = len(exps)
            run.quality_passed = sum(
                1 for e in exps if getattr(e, "success", False)
            )
            results = [
                {"rule": getattr(e, "expectation_type", str(e)),
                 "success": getattr(e, "success", None)}
                for e in exps
            ]
        run.quality_failed = run.quality_total - run.quality_passed
        run.quality_results = [
            {
                "rule": r.get("rule"),
                "column": r.get("column"),
                "success": r.get("success"),
                "kwargs": r.get("kwargs") or {},
                "unexpected_count": r.get("unexpected_count", 0),
                "unexpected_percent": r.get("unexpected_pct"),
                "partial_unexpected": r.get("partial_unexpected", []),
                "observed_value": r.get("observed_value"),
                "reason": _quality_fail_reason(r),
            }
            for r in results
        ]
        session.quality_passed = run.quality_passed
        session.quality_total = run.quality_total
    except Exception:
        pass


def _pii_to_dict(d: PIIDetection) -> dict:
    return {
        "column": d.column, "detected": d.detected, "entity_type": d.entity_type,
        "confidence": d.confidence, "presidio_score": d.presidio_score,
        "presidio_pattern": d.presidio_pattern, "gliner_score": d.gliner_score,
        "gliner_label": d.gliner_label, "llm_score": d.llm_score,
        "llm_verdict": d.llm_verdict, "llm_reasoning": d.llm_reasoning,
        "arabic_aware": d.arabic_aware, "arabic_fraction": d.arabic_fraction,
        "triage_score": d.triage_score, "decision_path": d.decision_path,
        "sample_match_rate": d.sample_match_rate,
    }


def execute_profile_step(
    session: ScanSession,
    config: ScanConfig,
    store: Optional[ContractStore] = None,
    backend: Optional[StorageBackend] = None,
):
    from redibis.scan import Scan
    from redibis.contracts.schema_base import build_schema_base
    from redibis.contracts.type_inference import dtype_map_from_dataframe

    session.set_status("profiling")
    session.config = config
    session.add_log(f"Starting profiling for {session.table_name}")
    df = _filter_session_df(session, config, _load_dataframe(session))
    session.total_columns = len(df.columns)
    session_dir = _session_dir(session)

    scan_config = replace(config, run_profile=True, run_quality=False, run_pii=False)
    from redibis.telemetry.llm_evidence import llm_evidence_recorder

    rid = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    run_art_dir = session_dir / "runs" / rid
    run_art_dir.mkdir(parents=True, exist_ok=True)
    run_result = None
    storage = backend or (getattr(store, "backend", None) if store is not None else None)
    try:
        with llm_evidence_recorder(
            run_dir=run_art_dir,
            run_id=rid,
            table=session.table_name,
            execution_mode="deterministic",
            spool_dir=session_dir / "_restricted_evidence",
        ):
            run_result = Scan(scan_config).run(df, run_id=rid, log_fn=session.add_log)
    finally:
        _flush_session_evidence(session, run_result, scan_config, run_art_dir, df, storage)
    if run_result is None or run_result.status != "success":
        raise RuntimeError(
            (run_result.error if run_result is not None else None) or "Profiling failed"
        )
    session.profiler = run_result.profile

    if store is not None and len(df.columns):
        col_dtypes = dtype_map_from_dataframe(df)
        profiles = getattr(run_result.profile, "column_profiles", None) if run_result.profile else None
        partial = build_schema_base(
            session.table_name,
            col_dtypes,
            df=df,
            profiles=profiles,
        )
        if run_result.profile and run_result.profile.source_metadata:
            from redibis.profiling.metadata_tiers import apply_catalog_props_to_partial

            partial = apply_catalog_props_to_partial(
                partial, run_result.profile.source_metadata,
            )
        upsert = store.upsert(
            partial=partial,
            table=session.table_name,
            workflow="schema",
            run_id=f"schema_{session.session_id[:12]}",
            validate=config.validate_contracts,
        )
        session.schema_contract_version = upsert.version_after
        session.add_log(
            f"Schema bootstrap: {len(col_dtypes)} column(s) inserted as normal columns "
            f"→ v{upsert.version_after}"
        )

    if store is not None and run_result.profile:
        fps = getattr(run_result.profile, "structural_fingerprints", None) or []
        if fps:
            from redibis.store.fingerprint_store import FingerprintStore
            fp_store = FingerprintStore.from_env(store.backend, store.bucket)
            fp_store.write_table(session.table_name, fps)
            session.add_log(f"Wrote {len(fps)} column fingerprint(s) to metadata store.")

    session.set_status("profiling_complete")
    session.add_log("Profiling completed.")
    session.persist_to_disk()


def execute_quality_step(
    session: ScanSession,
    backend: StorageBackend,
    store: ContractStore,
    *,
    finalize: bool = True,
):
    from redibis.scan import Scan

    run = session.start_run("scan_quality")
    try:
        session.set_status("running_quality")
        if not session.profiler:
            session.add_log("ERROR: Must profile first.")
            session.set_status("error")
            run.finish("error", "profiler not run")
            return
        config = session.config or ScanConfig(table=session.table_name)
        df = _filter_session_df(session, config, _load_dataframe(session))
        session_dir = _session_dir(session)
        run_art_dir = session_dir / "runs" / run.run_id
        run_art_dir.mkdir(parents=True, exist_ok=True)

        scan_config = replace(
            config,
            run_profile=False,
            run_quality=True,
            run_pii=False,
            quality_rule_set=session.common_config.quality_rule_set,
        )
        scan = Scan(scan_config)
        scan._profile = session.profiler
        from redibis.telemetry.llm_evidence import llm_evidence_recorder

        run_result = None
        flushed = {}
        try:
            with llm_evidence_recorder(
                run_dir=run_art_dir,
                run_id=run.run_id,
                table=session.table_name,
                execution_mode="deterministic",
                spool_dir=session_dir / "_restricted_evidence",
            ):
                run_result = scan.run(
                    df,
                    run_id=run.run_id,
                    run_dir=session_dir,
                    log_fn=session.add_log,
                )
        finally:
            flushed = _flush_session_evidence(
                session, run_result, scan_config, run_art_dir, df, backend,
            )
        if run_result is None or run_result.status != "success":
            raise RuntimeError(
                (run_result.error if run_result is not None else None) or "quality scan failed"
            )

        session.quality_gatekeeper = run_result.quality_gatekeeper
        if flushed.get("pii_detections_html"):
            session.artifacts["pii_detection_report"] = flushed["pii_detections_html"]
        run.artifacts["quality_report"] = flushed.get("quality_report", "")

        from redibis.store.storage_backend import _sanitize_for_yaml
        quality_contract = _sanitize_for_yaml(run_result.quality_contract or {})
        quality_contract_run = run_art_dir / "quality_contract.yaml"
        with open(quality_contract_run, "w", encoding="utf-8") as f:
            yaml.safe_dump(quality_contract, f, default_flow_style=False,
                           sort_keys=False, allow_unicode=True)
        run.artifacts["quality_contract"] = str(quality_contract_run)

        _sync_quality_stats(
            session, run, run_result.quality_gatekeeper, run_result.quality_results,
        )
        sub = session.add_sub_contract(
            kind="quality", content=quality_contract,
            run_id=run.run_id, source="scan",
        )
        upsert = _write_run_subcontract(
            session, backend, store, kind="quality",
            payload=quality_contract, run=run,
            summary_stats={
                "quality_passed": session.quality_passed,
                "quality_total": session.quality_total,
            },
            automerge=session.common_config.automerges("quality"),
        )
        if upsert is not None:
            session.quality_contract_version = upsert.version_after
            run.artifacts["merged_contract_version"] = upsert.version_after
            sub.status = "merged"
            sub.merged_version = upsert.version_after
            session.add_log(f"Quality run auto-merged → v{upsert.version_after}")
        else:
            session.add_log("Quality run written to bucket (automerge off — select & merge later).")

        run.finish("complete")
        session.add_log("Quality checks completed.")
        if finalize:
            session.set_status("quality_complete")
            _generate_run_report(session)
        session.persist_to_disk()
    except Exception as e:
        import traceback
        session.set_status("error")
        session.add_log(f"ERROR: {e}")
        run.finish("error", str(e))
        run.logs.append(traceback.format_exc())
        session.persist_to_disk()


def execute_pii_step(
    session: ScanSession,
    backend: StorageBackend,
    store: ContractStore,
    *,
    finalize: bool = True,
):
    from redibis.scan import Scan
    from redibis.store.storage_backend import _sanitize_for_yaml

    run = session.start_run("scan_pii")
    try:
        session.set_status("running_pii")
        session.add_log(f"Starting PII detection for {session.table_name}")
        config = session.config or ScanConfig(table=session.table_name)
        if session.common_config.regex_set:
            config = replace(
                config,
                pii_regex_overrides=session.common_config.regex_set.to_overrides(),
            )
        df = _filter_session_df(session, config, _load_dataframe(session))
        session.total_columns = len(df.columns)
        session_dir = _session_dir(session)
        run_art_dir = session_dir / "runs" / run.run_id
        run_art_dir.mkdir(parents=True, exist_ok=True)

        scan_config = replace(config, run_profile=False, run_quality=False, run_pii=True)
        scan = Scan(scan_config)
        from redibis.telemetry.llm_evidence import llm_evidence_recorder

        run_result = None
        flushed = {}
        try:
            with llm_evidence_recorder(
                run_dir=run_art_dir,
                run_id=run.run_id,
                table=session.table_name,
                execution_mode="deterministic",
                spool_dir=session_dir / "_restricted_evidence",
            ):
                run_result = scan.run(
                    df,
                    run_id=run.run_id,
                    run_dir=session_dir,
                    log_fn=session.add_log,
                )
        finally:
            flushed = _flush_session_evidence(
                session, run_result, scan_config, run_art_dir, df, backend,
            )
        if run_result is None or run_result.status != "success":
            raise RuntimeError(
                (run_result.error if run_result is not None else None) or "PII scan failed"
            )

        final = run_result.pii_detections
        session.pii_detections = final
        if flushed.get("pii_detections_html"):
            session.artifacts["pii_detection_report"] = flushed["pii_detections_html"]
        run.artifacts["pii_regex_review"] = flushed.get("pii_regex_review", "")
        run.artifacts["pii_detection_report"] = flushed.get(
            "pii_detection_report",
            flushed.get("pii_detections_html", flushed.get("pii_detections", "")),
        )

        pii_contract = _sanitize_for_yaml(run_result.pii_contract or {})
        pii_contract_run = run_art_dir / "pii_contract.yaml"
        with open(pii_contract_run, "w", encoding="utf-8") as f:
            yaml.safe_dump(pii_contract, f, default_flow_style=False, sort_keys=False)
        run.artifacts["pii_contract"] = str(pii_contract_run)

        sub = session.add_sub_contract(
            kind="pii", content=pii_contract,
            run_id=run.run_id, source="scan",
        )
        confirmed = [d for d in final if d.detected]
        upsert = _write_run_subcontract(
            session, backend, store, kind="pii",
            payload=pii_contract, run=run,
            summary_stats={
                "pii_confirmed": len(confirmed),
                "total_columns": len(final),
            },
            automerge=session.common_config.automerges("pii"),
        )
        if upsert is not None:
            session.pii_contract_version = upsert.version_after
            run.artifacts["merged_contract_version"] = upsert.version_after
            sub.status = "merged"
            sub.merged_version = upsert.version_after
            session.add_log(f"PII run auto-merged → v{upsert.version_after}")
        else:
            session.add_log("PII run written to bucket (automerge off — select & merge later).")

        signals = [d for d in final if not d.detected and (d.presidio_score or d.gliner_score)]
        run.pii_confirmed = len(confirmed)
        run.pii_signals = len(signals)
        run.pii_total_scanned = len(final)
        run.pii_detections = [_pii_to_dict(d) for d in final]
        session.pii_detected_count = len(confirmed)
        det_path = run_art_dir / "pii_detections.json"
        if flushed.get("pii_detections"):
            # Keep the rich flush artifact; do not replace it with a slim dict.
            pass
        elif not det_path.is_file():
            from redibis.scan.report_bundle import _detection_to_dict

            with open(det_path, "w", encoding="utf-8") as f:
                json.dump([_detection_to_dict(d) for d in final], f, indent=2, default=str)
        run.finish("complete")
        session.add_log("PII detection completed.")
        if finalize:
            session.set_status("pii_complete")
            _generate_run_report(session)
        session.persist_to_disk()
    except Exception as e:
        import traceback
        session.set_status("error")
        session.add_log(f"ERROR: {e}")
        run.finish("error", str(e))
        run.logs.append(traceback.format_exc())
        session.persist_to_disk()


def execute_unified_scan(session: ScanSession, backend: StorageBackend, store: ContractStore):
    """Single scan entry point — runs quality and/or PII based on global_config.scan_mode.

    Rebuilds the engine-facing ScanConfig from the current global config on
    every call, so edits made to the embedded regex/quality lists between runs
    are always picked up (edit-then-rerun).
    """
    mode = session.common_config.scan_mode
    # Pick up env / sole-model auto-resolution so scans work without manual Activate.
    resolved_ner = _effective_ner_model_path(session.common_config)
    if resolved_ner and not (session.common_config.pii_gliner_model or "").strip():
        session.common_config.pii_gliner_model = resolved_ner
        if not session.common_config.active_ner_model_name:
            session.common_config.active_ner_model_name = Path(resolved_ner).name
    session.config = _build_scan_config(
        session.table_name, session.common_config, Path(session.data_path).parent.parent,
    )
    session.add_log(f"Unified scan: mode={mode}")
    try:
        execute_profile_step(session, session.config, store, backend=backend)
        if mode in ("quality", "both"):
            execute_quality_step(
                session, backend, store, finalize=(mode == "quality"),
            )
        if mode in ("pii", "both"):
            execute_pii_step(
                session, backend, store, finalize=(mode == "pii"),
            )
        session.set_status("scan_complete")
        session.add_log("Unified scan complete.")
        _generate_run_report(session)
        session.persist_to_disk()
    except Exception as e:
        import traceback
        session.set_status("error")
        session.add_log(f"Scan failed: {e}")
        session.add_log(traceback.format_exc())
        session.persist_to_disk()
    finally:
        _write_session_evidence_index(session)


def _effective_discovery_config(session: ScanSession, overrides: Optional[dict]) -> "GlobalConfig":
    """Clone the session's global config and apply per-run discovery overrides.

    Discovery is decoupled from the main scan: it tests specific rules / a
    different NER model / an ad-hoc regex on a small subset, WITHOUT mutating
    the session's global config.
    """
    import copy
    cfg = copy.deepcopy(session.common_config)
    if not overrides:
        return cfg
    scalar_overrides = (
        "pii_engines", "pii_gliner_model", "pii_regex_confidence",
        "pii_gliner_confidence", "pii_llm_confidence", "pii_gliner_always_run",
        "equation_mode",
    )
    for k in scalar_overrides:
        if overrides.get(k) is not None:
            setattr(cfg, k, overrides[k])
    if overrides.get("regex_patterns") is not None:
        from redibis.pii.regex_overrides import RegexSet
        cfg.regex_set = RegexSet(patterns=dict(overrides["regex_patterns"]),
                                 replace_all=bool(overrides.get("regex_replace_all", False)))
    return cfg


def execute_discovery_run(session: ScanSession, kind: str,
                          columns: Optional[List[str]] = None,
                          subset_rows: Optional[int] = None,
                          rules: Optional[List[dict]] = None,
                          overrides: Optional[dict] = None):
    """Run a scoped discovery scan (PII *or* quality) over the real data.

    Decoupled from the main scan button: tests one/some rules (regex, quality)
    or a different NER model on a small column subset. Produces a DiscoveryRun
    (with its own run_id) attached to the session's DiscoverySession and writes
    NO contracts — results are for the discovery page only.
    """
    from redibis.services.discovery_service import DiscoverySession, DiscoveryRun
    if kind not in ("pii", "quality"):
        raise ValueError(f"discovery kind must be 'pii' or 'quality', got {kind!r}")
    if session.discovery is None:
        session.discovery = DiscoverySession()
    cfg = _effective_discovery_config(session, overrides)
    run_id = f"discovery_{kind}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{str(uuid.uuid4())[:6]}"
    drun = DiscoveryRun(run_id=run_id, kind=kind, columns=columns or None,
                        subset_rows=subset_rows, config_snapshot=cfg.to_dict())
    session.discovery.runs.append(drun)
    session.add_log(f"Discovery run started: {run_id} (kind={kind}, "
                    f"columns={columns or 'all'}, subset={subset_rows or 'all'})")
    try:
        df = _load_dataframe(session)
        if columns:
            valid = [c for c in columns if c in df.columns]
            if valid:
                df = df[valid]
        if subset_rows and len(df) > subset_rows:
            df = df.sample(n=subset_rows, random_state=42)
        if kind == "pii":
            _discovery_pii(df, drun, cfg)
        else:
            _discovery_quality(session, df, drun, rules, cfg)
        drun.finish("complete")
        session.add_log(f"Discovery run complete: {run_id} ({drun.summary})")
    except Exception as e:
        import traceback
        drun.finish("error", str(e))
        session.add_log(f"Discovery run error: {e}")
        log.warning("Discovery run %s failed: %s", run_id, traceback.format_exc())
    return drun


def _discovery_pii(df: pd.DataFrame, drun, cfg: "GlobalConfig") -> None:
    from redibis.services import pipeline
    from redibis.config import GlinerConfig, NERConfig
    from redibis.services.session.config import _effective_ner_model_path
    overrides = cfg.regex_set.to_overrides() if cfg.regex_set else None
    ner_model_path = _effective_ner_model_path(cfg)
    thresholds = pipeline.thresholds_from(
        cfg.pii_regex_confidence, cfg.pii_gliner_confidence, cfg.pii_llm_confidence)
    from redibis.config import resolve_pii_config
    from redibis.services.session.config import from_global_config

    pii_config = resolve_pii_config(from_global_config(cfg).pii)
    final = pipeline.run_pii_detection(
        df, columns=None, engines=cfg.pii_engines,
        gliner_always_run=cfg.pii_gliner_always_run,
        ner_always_run=cfg.pii_gliner_always_run,
        regex_overrides=overrides,
        ner_config=NERConfig(
            model_path=ner_model_path,
            always_run=cfg.pii_gliner_always_run,
        ),
        gliner_config=GlinerConfig(model_id=ner_model_path),
        equation_mode=cfg.equation_mode, thresholds=thresholds,
        pii_config=pii_config,
    )
    drun.results = [_pii_to_dict(d) for d in final]
    drun.summary = {
        "columns_scanned": len(final),
        "detected": sum(1 for d in final if d.detected),
        "signals": sum(1 for d in final if not d.detected and (d.presidio_score or d.gliner_score)),
    }


def _discovery_quality(session: ScanSession, df: pd.DataFrame, drun,
                       rules: Optional[List[dict]], cfg: "GlobalConfig") -> None:
    from redibis.services import pipeline
    from redibis.services.discovery_service import DiscoveryService, QualityProbe
    from redibis.quality.rule_set import QualityRuleSet
    db_name, tbl_name = split_table(session.table_name)
    profile = pipeline.profile_dataframe(
        df, tbl_name, triage_threshold=cfg.triage_threshold,
    )
    rule_list = list(rules) if rules else list(cfg.quality_rule_set.rules or [])
    ge_rules = [r for r in rule_list if r.get("rule") != "sql" and not r.get("sql")]
    sql_rules = [r for r in rule_list if r.get("rule") == "sql" or r.get("sql")]

    merged: list[dict] = []
    if sql_rules:
        svc = DiscoveryService()
        for r in sql_rules:
            probe = QualityProbe(
                rule="sql",
                column=r.get("column"),
                sql=r.get("sql") or r.get("kwargs", {}).get("sql"),
                kwargs=dict(r.get("kwargs") or {}),
            )
            pr = svc.probe_quality(probe, df)
            merged.append({
                "rule": "sql",
                "column": r.get("column"),
                "success": pr.success,
                "kwargs": r.get("kwargs") or {},
                "unexpected_count": pr.unexpected_count,
                "reason": (
                    ""
                    if pr.success
                    else (
                        f"{pr.unexpected_count} violation(s)"
                        if pr.unexpected_count
                        else str(pr.observed_value or "check failed")
                    )
                ),
            })

    if ge_rules or not rule_list:
        qa = QualityGatekeeper(suite_name=f"{tbl_name}_discovery_suite", in_memory=True)
        qa.attach_dataframe(df, dataset_name=tbl_name)
        rule_set = QualityRuleSet(rules=ge_rules) if ge_rules else cfg.quality_rule_set
        pipeline.apply_quality_rules(qa, rule_set, profile.expectations)
        quality_results = qa.run_tests(stage="discovery", generate_docs=False)
        report = qa._extract_report_data(quality_results)
        for r in report.get("results", []):
            merged.append({
                "rule": r.get("rule"),
                "column": r.get("column"),
                "success": r.get("success"),
                "kwargs": r.get("kwargs") or {},
                "unexpected_count": r.get("unexpected_count", 0),
                "reason": _quality_fail_reason(r),
            })

    drun.results = merged
    passed = sum(1 for r in merged if r.get("success"))
    drun.summary = {
        "total": len(merged),
        "passed": passed,
        "failed": len(merged) - passed,
    }


def _write_run_subcontract(session: ScanSession, backend: StorageBackend,
                           store: ContractStore, kind: str, payload: dict,
                           run: RunRecord, summary_stats: Optional[dict] = None,
                           *, automerge: bool = False):
    """Persist a scan run's partial into its v2 type bucket (source of truth)."""
    from redibis.scan.contract_writer import ScanContractWriter
    from redibis.store.run_merger import RunMerger

    try:
        sub_store = build_subcontract_store(backend)
        active = store.get_active(session.table_name)
        contract_uuid = active.get("contract_uuid") if active else None
        merger = RunMerger(store, sub_store)
        sub, upsert = ScanContractWriter.persist_session_kind(
            kind,
            table=session.table_name,
            run_id=run.run_id,
            payload=payload,
            sub_store=sub_store,
            store=store,
            merger=merger,
            contract_uuid=contract_uuid,
            summary_stats=summary_stats,
            automerge=automerge,
            validate=(session.config or ScanConfig(table=session.table_name)).validate_contracts,
        )
        run.artifacts[f"{kind}_subcontract_id"] = sub.subcontract_id
        session.add_log(f"{kind} run written to {kind}-contracts bucket "
                        f"({session.table_name}/{run.run_id}.yaml)")
        if upsert is not None:
            try:
                from redibis.contracts.lifecycle import (
                    append_lifecycle_provenance,
                    build_engine_set,
                    write_deterministic_snapshot,
                )
                from redibis.store.run_output_writer import RunOutputWriter

                c_det = store.get_active(session.table_name)
                scan_cfg = session.config or ScanConfig(table=session.table_name)
                cc = session.common_config
                use_phone = cc.use_phonenumbers
                if use_phone is None:
                    use_phone = True
                if c_det is not None:
                    run_writer = RunOutputWriter(
                        backend=backend,
                        bucket=os.getenv("S3_RUNS_BUCKET", "pii-reports"),
                        workflow="scan",
                        table=session.table_name,
                        run_id=run.run_id,
                    )
                    write_deterministic_snapshot(
                        run_writer, c_det, run_id=run.run_id,
                        version=c_det.get("version"),
                    )
                    append_lifecycle_provenance(
                        store.metadata,
                        session.table_name,
                        workflow=kind,
                        run_id=run.run_id,
                        active_source="deterministic",
                        engines=build_engine_set(
                            pii_engines=cc.pii_engines,
                            use_phonenumbers=use_phone,
                            equation_mode=cc.equation_mode,
                        ),
                        det_audit_version=c_det.get("version"),
                    )
            except Exception as e:
                session.add_log(f"WARN: deterministic lifecycle snapshot failed: {e}")
            return upsert
    except Exception as e:
        session.add_log(f"WARN: failed to write {kind} run subcontract: {e}")
    return None


def merge_session_sub_contracts(session: ScanSession, store: ContractStore,
                                ids: Optional[List[str]] = None,
                                validate: Optional[bool] = None) -> List[dict]:
    """Manually merge selected staged sub-contracts into the active contract.

    This is the web flow's explicit "merge when satisfied" action. Each
    selected sub-contract is upserted (workflow = its kind) via the
    ContractStore — still the only writer to the contracts bucket.
    """
    if validate is None:
        validate = bool(session.common_config.validate_contracts)
    selected = (
        [s for s in session.sub_contracts if s.sub_id in set(ids)] if ids
        else [s for s in session.sub_contracts if s.status == "staged"]
    )
    if not selected:
        return []
    workflow_map = {"pii": "pii", "quality": "quality",
                    "business": "business", "manual": "manual"}
    results = []
    for sc in selected:
        workflow = workflow_map.get(sc.kind, "manual")
        upsert = store.upsert(partial=dict(sc.content), table=sc.table,
                              workflow=workflow, run_id=sc.run_id or sc.sub_id,
                              validate=validate)
        sc.status = "merged"
        sc.merged_version = upsert.version_after
        if sc.kind == "pii":
            session.pii_contract_version = upsert.version_after
        elif sc.kind == "quality":
            session.quality_contract_version = upsert.version_after
        results.append({"sub_id": sc.sub_id, "kind": sc.kind, "table": sc.table,
                        "version_after": upsert.version_after, "is_new": upsert.is_new})
        session.add_log(f"Sub-contract merged: {sc.sub_id} → v{upsert.version_after}")
    return results


def _file_sha256(path: Path) -> str:
    import hashlib

    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _sub_run_status(man: Path, child: Path) -> str:
    if man.is_file():
        try:
            data = json.loads(man.read_text(encoding="utf-8")) or {}
            return str(data.get("run_status") or "")
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    return ""


def _sub_run_completed_at(man: Path, child: Path) -> str:
    if man.is_file():
        try:
            data = json.loads(man.read_text(encoding="utf-8")) or {}
            return str(data.get("created_at") or "")
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    try:
        return datetime.fromtimestamp(child.stat().st_mtime, timezone.utc).isoformat()
    except OSError:
        return ""


def _write_session_evidence_index(session: ScanSession) -> None:
    """Link profile/quality/PII sub-run manifests into one session index."""
    session_dir = _session_dir(session)
    runs_dir = session_dir / "runs"
    links: list[dict] = []
    if runs_dir.is_dir():
        for child in sorted(p for p in runs_dir.iterdir() if p.is_dir()):
            man = child / "evidence_manifest.json"
            bundle = child / "evidence_bundle.json"
            if not man.is_file() and not bundle.is_file():
                continue
            rel_man = ""
            try:
                if man.is_file():
                    rel_man = man.relative_to(session_dir).as_posix()
            except ValueError:
                rel_man = str(man)
            links.append({
                "run_id": child.name,
                "status": _sub_run_status(man, child),
                "completed_at": _sub_run_completed_at(man, child),
                "manifest": rel_man,
                "manifest_sha256": _file_sha256(man) if man.is_file() else "",
                "evidence_bundle": (
                    (child / "evidence_bundle.json").relative_to(session_dir).as_posix()
                    if bundle.is_file() else ""
                ),
            })
    payload = {
        "kind": "redibis.session_evidence",
        "schema_version": "1.0",
        "session_id": session.session_id,
        "table": session.table_name,
        "status": session.status,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "sub_runs": links,
    }
    try:
        from redibis.evidence.persist import atomic_write_json

        atomic_write_json(session_dir / "evidence_manifest.json", payload)
    except OSError as exc:
        session.add_log(f"Warning: session evidence index failed: {exc}")


def _generate_run_report(session: ScanSession) -> None:
    run_dir = Path(session.data_path).parent
    output_path = run_dir / "run_report.html"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    sid = session.session_id
    pii_confirmed = [d for d in session.pii_detections if d.detected]
    pii_signals = [d for d in session.pii_detections if not d.detected and (d.presidio_score or d.gliner_score)]
    pii_total = len(session.pii_detections)
    base = f"/api/sessions/{sid}/artifacts"
    art = session.artifacts
    def art_row(name, label, icon):
        if name in art:
            return f"<a href='{base}/{name}' target='_blank' class='art-card'><span class='ai'>{icon}</span><span class='an'>{label}</span><span class='al'>open &#8594;</span></a>"
        return f"<div class='art-card off'><span class='ai'>{icon}</span><span class='an'>{label}</span><span class='al'>&#8212;</span></div>"
    pii_rows = ""
    for d in session.pii_detections:
        chip = ("<span class='chip chip-red'>&#10003; PII</span>" if d.detected else
                "<span class='chip chip-amber'>&#126; signal</span>" if (d.presidio_score or d.gliner_score) else
                "<span class='chip chip-muted'>clean</span>")
        ps = f"{d.presidio_score:.3f}" if d.presidio_score else "&#8212;"
        gs = f"{d.gliner_score:.3f}" if d.gliner_score else "&#8212;"
        pii_rows += f"<tr><td>{d.column}</td><td>{chip}</td><td>{d.entity_type or '&#8212;'}</td><td>{ps}</td><td>{gs}</td></tr>"
    runs_rows = ""
    for r in session.runs:
        dur = f"{r.duration_seconds}s" if r.duration_seconds else "&#8212;"
        sc = {"complete":"chip-grn","error":"chip-red","running":"chip-amb"}.get(r.status,"chip-muted")
        runs_rows += f"<tr><td class='mono' style='font-size:.72rem'>{r.run_id}</td><td>{r.run_type}</td><td><span class='chip {sc}'>{r.status}</span></td><td>{dur}</td><td>{r.pii_confirmed}/{r.pii_total_scanned}</td><td>{r.quality_passed}/{r.quality_total}</td></tr>"
    html = f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"/><title>Session Report</title>
<style>:root{{--ink:#0f172a;--red:#e03131;--amber:#f59e0b;--green:#22c55e;--bg:#f8fafc;--surface:#fff;--border:#e2e8f0;--muted:#64748b}}
*{{box-sizing:border-box;margin:0;padding:0}}body{{font-family:'Inter',system-ui,sans-serif;background:var(--bg);color:var(--ink)}}
.header{{background:var(--ink);color:#fff;padding:1.5rem 2rem;display:flex;justify-content:space-between;align-items:flex-end}}
.header h1{{font-size:1.5rem;font-weight:800}}.section{{padding:1.25rem 2rem;border-bottom:1px solid var(--border)}}
.st{{font-size:.65rem;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin-bottom:.75rem}}
.art-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:.75rem}}
.art-card{{display:flex;align-items:center;gap:.75rem;padding:.75rem 1rem;background:var(--surface);border:1px solid var(--border);border-radius:8px;text-decoration:none;color:var(--ink)}}
.art-card.off{{opacity:.4}}.ai{{font-size:1.3rem}}.an{{flex:1;font-weight:600;font-size:.85rem}}.al{{font-size:.75rem;color:var(--red);font-weight:700}}
table{{width:100%;border-collapse:collapse;font-size:.8rem}}
th{{padding:.55rem .75rem;font-size:.62rem;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);border-bottom:2px solid var(--border);text-align:left;font-weight:700}}
td{{padding:.55rem .75rem;border-bottom:1px solid var(--border)}}
.chip{{display:inline-block;padding:.12rem .45rem;border-radius:999px;font-size:.62rem;font-weight:700}}
.chip-red{{background:#fee2e2;color:#991b1b}}.chip-amber{{background:#fef3c7;color:#92400e}}
.chip-muted{{background:#f1f5f9;color:#64748b}}.chip-grn{{background:#dcfce7;color:#166534}}
.mono{{font-family:'IBM Plex Mono',monospace}}</style></head><body>
<div class="header"><div><h1>Session Report</h1><div style="color:#94a3b8;margin-top:.25rem">{session.table_name}</div></div>
<div style="font-size:.78rem;color:#94a3b8;text-align:right;line-height:1.7">Session: <strong>{sid[:8]}&#8230;</strong><br/>Generated: {ts}<br/>Runs: {len(session.runs)}</div></div>
<div class="section"><div class="st">Artifacts</div><div class="art-grid">
{art_row('pii_detections','PII Detection Report','&#128269;')}
{art_row('pii_regex_review','Regex Review','&#9881;')}
{art_row('interactive_review','Quality Review (Interactive)','&#128202;')}
{art_row('quality_report','Quality Report','&#128196;')}
{art_row('pii_contract','PII Contract','&#128274;')}
{art_row('quality_contract','Quality Contract','&#9989;')}
</div></div>
<div class="section"><div class="st">Run History ({len(session.runs)} runs)</div>
<table><thead><tr><th>Run ID</th><th>Type</th><th>Status</th><th>Duration</th><th>PII conf/scanned</th><th>Quality pass/total</th></tr></thead>
<tbody>{runs_rows or '<tr><td colspan="6" style="text-align:center;color:var(--muted);padding:1rem">No runs yet</td></tr>'}</tbody></table></div>
<div class="section"><div class="st">PII Summary</div>
{'<table><thead><tr><th>Column</th><th>Status</th><th>Entity</th><th>Presidio</th><th>GLiNER</th></tr></thead><tbody>'+pii_rows+'</tbody></table>' if pii_total else '<p style="color:var(--muted);font-style:italic">No PII scan completed yet.</p>'}</div>
</body></html>"""
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    session.artifacts["run_report"] = str(output_path)
    session.add_log("Run report generated.")
