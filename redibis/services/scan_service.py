"""
redibis.services.scan_service
=============================
ScanService — orchestrates the full scan pipeline from a file/DataFrame
through quality profiling + PII detection to S3 storage.

This is a pure business logic class. It has NO knowledge of REST, CLI,
or any presentation layer. REST endpoints and CLI commands are thin
adapters that call this service.

Pipeline steps
--------------
    1. Parse input (CSV path → DataFrame)
    2. Quality profiling (GE OnboardingDataAssistant)
    3. Arabic detection + column triage
    4. Quality gatekeeper (run expectations, generate reports)
    5. PII detection (Presidio + GLiNER on triaged columns)
    6. Equation application (strict/balanced/lenient)
    7. Contract generation (quality + PII partials)
    8. Save all outputs to S3 (RunOutputWriter)
    9. Upsert contracts to ContractStore
    10. Return ScanResult with links to all artifacts
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional, Union, Callable, TYPE_CHECKING

import pandas as pd

from redibis.config import GlinerConfig, LLMConfig, NERConfig
from redibis.models import PIIDetection
from redibis.pii.ner_backend import NERBackend
from redibis.quality.rule_set import QualityRuleSet
from redibis.quality.sampling import SamplingConfig
from redibis.pii.thresholds import Thresholds
from redibis.pii.regex_overrides import RegexOverrides
from redibis.store.contract_store import ContractStore
from redibis.store.storage_backend import StorageBackend
from redibis.store.run_output_writer import RunOutputWriter
from redibis.store.subcontract_store import SubcontractStore
from redibis.store.run_merger import RunMerger

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from redibis.config import RedibisConfig


# ─────────────────────────────────────────────────────────────────────────────
# Result dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ScanResult:
    """Result of a full scan pipeline run."""
    run_id:                str
    table:                 str
    session_id:            Optional[str]              = None
    status:                str                        = "pending"
    error:                 Optional[str]              = None

    # Counts
    total_rows:            int                        = 0
    total_columns:         int                        = 0
    quality_expectations:  int                        = 0
    quality_passed:        int                        = 0
    quality_failed:        int                        = 0
    pii_columns_scanned:   int                        = 0
    pii_columns_detected:  int                        = 0
    arabic_aware_columns:  int                        = 0

    # Contract versions
    quality_contract_version: Optional[str]           = None
    pii_contract_version:     Optional[str]           = None

    # Generated file links (S3 keys or local paths)
    artifacts:             dict[str, str]             = field(default_factory=dict)

    # Logs
    scan_log:              list[str]                  = field(default_factory=list)
    detailed_log:          list[str]                  = field(default_factory=list)

    # Timing
    started_at:            Optional[str]              = None
    completed_at:          Optional[str]              = None
    duration_seconds:      Optional[float]            = None
    evidence_warnings:     list                       = field(default_factory=list)

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


# ─────────────────────────────────────────────────────────────────────────────
# Scan configuration
# ─────────────────────────────────────────────────────────────────────────────

# ScanConfig moved to redibis.scan.config (engine config). Re-exported at bottom.


# ─────────────────────────────────────────────────────────────────────────────
# ScanService
# ─────────────────────────────────────────────────────────────────────────────

class ScanService:
    """
    Scan **service** -- the file/S3/contract layer around the scan engine.

    NOT the engine: this wraps ``redibis.scan.Scan`` (the in-memory engine) and returns a
    ``ScanResult`` (service result; engine twin: ``EngineScanResult``). It composes all
    redibis modules but contains NO presentation logic. REST and CLI adapters call this service.

    Usage (from code):
        service = ScanService(backend=backend, store=store)
        result = service.scan_csv(
            csv_path = "data.csv",
            config   = ScanConfig(table="telecom.customers"),
        )
        print(result.artifacts)

    Usage (from DataFrame):
        result = service.scan_dataframe(
            df     = my_dataframe,
            config = ScanConfig(table="telecom.customers"),
        )
    """

    def __init__(
        self,
        backend:        StorageBackend,
        store:          ContractStore,
        runs_bucket:    str = "pii-reports",
        sub_store:      Optional[SubcontractStore] = None,
    ) -> None:
        self.backend      = backend
        self.store         = store
        self.runs_bucket   = runs_bucket
        # v2: subcontracts are the source of truth; scan writes here, not upsert.
        self.sub_store     = sub_store or SubcontractStore(backend)
        self.run_merger    = RunMerger(store, self.sub_store)

    # ── Public entry points ───────────────────────────────────────────────

    def scan_csv(
        self,
        csv_path: Union[str, Path],
        config:   ScanConfig,
    ) -> ScanResult:
        """Scan a CSV file through the full pipeline."""
        df = pd.read_csv(csv_path, dtype=str, keep_default_na=False)
        return self.scan_dataframe(df=df, config=config)

    def scan_bytes(
        self,
        file_bytes: bytes,
        filename:   str,
        config:     ScanConfig,
    ) -> ScanResult:
        """Scan uploaded file bytes (CSV). Used by the REST endpoint."""
        if filename.endswith(".csv"):
            df = pd.read_csv(io.BytesIO(file_bytes), dtype=str, keep_default_na=False)
        elif filename.endswith((".parquet", ".pq")):
            df = pd.read_parquet(io.BytesIO(file_bytes))
        elif filename.endswith((".xlsx", ".xls")):
            df = pd.read_excel(io.BytesIO(file_bytes))
        else:
            df = pd.read_csv(io.BytesIO(file_bytes), dtype=str, keep_default_na=False)
        return self.scan_dataframe(df=df, config=config)

    def scan_dataframe(
        self,
        df:     pd.DataFrame,
        config: ScanConfig,
        *,
        redibis_config: Optional["RedibisConfig"] = None,
    ) -> ScanResult:
        """
        Core scan method. Runs the full pipeline on a DataFrame.

        Steps:
          1. Sample (if config.sampling_config provided)
          2. Profile (GE OnboardingDataAssistant + Arabic detection)
          3. Quality gatekeeper (run expectations)
          4. PII detection (if config.run_pii)
          5. Generate all reports
          6. Build contracts
          7. Save to S3
          8. Upsert to ContractStore
        """
        run_id  = config.run_id or datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")

        if config.artifacts_dir is not None:
            run_dir = Path(config.artifacts_dir)
        else:
            config.output_dir.mkdir(parents=True, exist_ok=True)
            run_dir = config.output_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        run_prefix = run_id
        if config.session_id:
            run_prefix = f"{config.session_id}/{run_id}"
        run_writer = RunOutputWriter(
            backend  = self.backend,
            bucket   = self.runs_bucket,
            workflow = "scan",
            table    = config.table,
            run_id   = run_prefix,
        )

        from redibis.services.run_logging import capture_run_log

        scan_log: list[str] = []
        run_result = None

        def _add_step(msg):
            scan_log.append(msg)
            log.info(msg)
            print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)

        result: Optional[ScanResult] = None
        detailed_lines: list[str] = []

        obs_persist = (
            redibis_config is None
            or redibis_config.observability.persist_run_log
        )
        if obs_persist:
            from redibis.obs import persist_run_log
            _log_cm = persist_run_log(run_dir, config.table, run_id)
        else:
            _log_cm = capture_run_log()

        try:
            from redibis.scan import Scan, ReportBundle, ScanContractWriter
            from redibis.telemetry.llm_evidence import llm_evidence_recorder

            with _log_cm as _log_capture:
                scan = (
                    Scan.from_redibis(redibis_config)
                    if redibis_config is not None
                    else Scan(config)
                )
                with llm_evidence_recorder(
                    run_dir=run_dir,
                    run_id=run_id,
                    table=config.table,
                    execution_mode="deterministic",
                    run_writer=run_writer,
                    config=redibis_config,
                ):
                    run_result = scan.run(
                        df,
                        run_id=run_id,
                        run_dir=run_dir,
                        log_fn=_add_step,
                    )

                    try:
                        _add_step("Exporting reports...")
                        ReportBundle.from_result(run_result, config).flush(
                            run_dir,
                            run_writer=run_writer,
                            df=df,
                        )
                    except Exception as flush_exc:
                        _add_step(f"Warning: evidence flush failed: {flush_exc}")
                        run_result.evidence_warnings.append(
                            {"phase": "flush", "error": str(flush_exc)}
                        )

                if run_result.status == "success":
                    if run_result.contract_draft is not None:
                        _add_step("Writing run subcontracts...")
                        persist = ScanContractWriter.persist(
                            run_result.contract_draft,
                            sub_store=self.sub_store,
                            store=self.store,
                            merger=self.run_merger,
                            config=config,
                            validate=config.validate_contracts,
                        )
                        run_result.quality_contract_version = persist.quality_version
                        run_result.pii_contract_version = persist.pii_version
                        if (config.automerge or "").lower() not in ("", "none"):
                            self._write_deterministic_lifecycle(
                                run_writer=run_writer,
                                table=config.table,
                                run_id=run_id,
                                scan_config=config,
                                redibis_config=redibis_config,
                            )
                        if config.automerges("quality") and persist.quality_version:
                            _add_step(
                                f"Quality run auto-merged → v{persist.quality_version}"
                            )
                        elif run_result.quality_contract:
                            _add_step(
                                "Quality run written to quality-contracts bucket (no merge)."
                            )
                        if config.automerges("pii") and persist.pii_version:
                            _add_step(f"PII run auto-merged → v{persist.pii_version}")
                        elif run_result.pii_contract:
                            _add_step("PII run written to pii-contracts bucket (no merge).")

                    if run_result.duration_seconds is not None:
                        _add_step(f"Duration: {run_result.duration_seconds:.1f}s")

                result = run_result.to_scan_result()
                result.scan_log = scan_log
                _log_capture.seek(0)
                detailed_lines = _log_capture.read().splitlines()

            _add_step("Writing run manifest...")
            try:
                run_writer.write("run_manifest.json", result.to_dict())
            except Exception as e:
                _add_step(f"Warning: Failed to write manifest to S3: {e}")
            result.scan_log = scan_log

        except Exception as e:
            if run_result is not None:
                result = run_result.to_scan_result()
                result.scan_log = scan_log
            else:
                result = ScanResult(
                    run_id=run_id,
                    table=config.table,
                    session_id=config.session_id,
                    started_at=datetime.now(timezone.utc).isoformat(),
                    scan_log=list(scan_log),
                )
            result.status = "failed"
            result.error  = str(e)
            result.completed_at = datetime.now(timezone.utc).isoformat()
            result.scan_log.append(f"ERROR: {e}")
            log.exception(f"Scan failed for {config.table}: {e}")
            if run_result is not None:
                try:
                    from redibis.scan.report_bundle import ReportBundle
                    ReportBundle.from_result(run_result, config).flush(
                        run_dir, run_writer=run_writer, df=df,
                    )
                except Exception as flush_exc:
                    _add_step(f"Warning: evidence flush failed: {flush_exc}")
            try:
                run_writer.write("run_manifest.json", result.to_dict())
            except Exception as man_exc:
                _add_step(f"Warning: Failed to write manifest to S3: {man_exc}")

        if result is not None:
            result.detailed_log = detailed_lines

        return result

    # ── Private helpers ───────────────────────────────────────────────────

    def _run_pii_detection(
        self,
        df:              pd.DataFrame,
        config:          ScanConfig,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> list[PIIDetection]:
        """Run PII detection + equation via the shared pipeline (single source of truth)."""
        from redibis.services import pipeline

        thresholds = config.thresholds or pipeline.thresholds_from(
            config.pii_regex_confidence,
            config.pii_gliner_confidence,
            config.pii_llm_confidence,
        )
        from dataclasses import replace
        from redibis.config import resolve_pii_config

        pii_cfg = resolve_pii_config()
        if config.use_phonenumbers is not None:
            pii_cfg = replace(pii_cfg, use_phonenumbers=config.use_phonenumbers)

        try:
            return pipeline.run_pii_detection(
                df,
                columns=config.pii_columns,
                engines=config.pii_engines,
                gliner_always_run=config.pii_gliner_always_run,
                ner_always_run=config.ner_always_run(),
                regex_overrides=config.pii_regex_overrides,
                ner_config=config.ner_config,
                gliner_config=config.gliner_config,
                ner_backend=config.ner_backend,
                equation_mode=config.equation_mode,
                thresholds=thresholds,
                progress_callback=progress_callback,
                pii_config=pii_cfg,
            )
        except (ImportError, NotImplementedError):
            log.warning(
                "PII detection engines not available. "
                "Install with: pip install redibis[ner]"
            )
            return []

    def _write_deterministic_lifecycle(
        self,
        *,
        run_writer: RunOutputWriter,
        table: str,
        run_id: str,
        scan_config: ScanConfig,
        redibis_config=None,
    ) -> None:
        """Snapshot C_det to the run folder after deterministic automerge."""
        if self.store is None:
            return
        c_det = self.store.get_active(table)
        if not c_det:
            return
        from redibis.contracts.lifecycle import (
            append_lifecycle_provenance,
            build_engine_set,
            write_deterministic_snapshot,
        )

        use_phone = True
        if redibis_config is not None:
            use_phone = redibis_config.pii.use_phonenumbers
        if scan_config.use_phonenumbers is not None:
            use_phone = scan_config.use_phonenumbers

        write_deterministic_snapshot(
            run_writer, c_det, run_id=run_id, version=c_det.get("version"),
        )
        append_lifecycle_provenance(
            self.store.metadata,
            table,
            workflow="schema",
            run_id=run_id,
            active_source="deterministic",
            engines=build_engine_set(
                pii_engines=scan_config.pii_engines,
                use_phonenumbers=use_phone,
                equation_mode=scan_config.equation_mode,
                profiler_engine=scan_config.profiler_engine,
            ),
            det_audit_version=c_det.get("version"),
        )

    def _upload_artifacts(
        self,
        run_writer: RunOutputWriter,
        run_dir:    Path,
    ) -> dict[str, str]:
        """Upload all files in run_dir to S3 via RunOutputWriter."""
        s3_links = {}
        for path in run_dir.rglob("*"):
            if path.is_file():
                relative = path.relative_to(run_dir).as_posix()
                try:
                    key = run_writer.write_file(relative, path)
                    s3_links[f"s3:{relative}"] = key
                except Exception as e:
                    log.warning(f"Failed to upload {relative}: {e}")
        return s3_links

    def _extract_stats(self, results: Any) -> dict:
        """Extract pass/fail counts from GE results (version-safe)."""
        try:
            for _key, val in results.run_results.items():
                if hasattr(val, "to_json_dict"):
                    vr = val.to_json_dict()
                elif isinstance(val, dict):
                    vr = val.get("validation_result", val)
                else:
                    continue
                stats = vr.get("statistics", {})
                return {
                    "total":  stats.get("evaluated_expectations", 0),
                    "passed": stats.get("successful_expectations", 0),
                    "failed": stats.get("unsuccessful_expectations", 0),
                }
        except Exception:
            pass
        return {}

    @staticmethod
    def _detection_to_dict(d: PIIDetection) -> dict:
        """Convert PIIDetection to a JSON-serializable dict."""
        from dataclasses import asdict
        try:
            return asdict(d)
        except Exception:
            return {
                "column": d.column, "detected": d.detected,
                "entity_type": d.entity_type, "confidence": d.confidence,
            }

    @staticmethod
    def _split_table(table: str) -> tuple[str, str]:
        if "." in table:
            db, tbl = table.split(".", 1)
            return db, tbl
        return "", table


# ── RedibisConfig ↔ ScanConfig adapters ─────────────────────────────────────

def to_scan_config(
    cfg: "RedibisConfig",
    *,
    table: Optional[str] = None,
    output_dir: Optional[Union[str, Path]] = None,
    auto_write: Optional[bool] = None,
) -> ScanConfig:
    """Project ``RedibisConfig`` → ``ScanConfig``."""
    from redibis.config import (
        ConfigError,
        RedibisConfig,
        _scan_types_to_flags,
    )
    from redibis.pii.regex_overrides import RegexSet

    run_pii, run_profile, run_quality = _scan_types_to_flags(cfg.scan_types)
    tbl = table or cfg.table
    if not tbl:
        raise ConfigError("table is required")

    automerge = cfg.contract.automerge
    if auto_write is True:
        import warnings
        warnings.warn(
            "auto_write no longer forces automerge; pass automerge explicitly",
            DeprecationWarning,
            stacklevel=2,
        )

    llm_cfg = None
    if cfg.pii.llm.enabled and cfg.pii.llm.api_key:
        llm_cfg = LLMConfig(
            provider=cfg.pii.llm.provider,
            model_name=cfg.pii.llm.model_name,
            api_key=cfg.pii.llm.api_key,
            endpoint_url=cfg.pii.llm.endpoint_url,
            temperature=cfg.pii.llm.temperature,
            enabled=True,
        )

    regex_overrides = None
    if cfg.pii.regex_overrides:
        regex_overrides = RegexSet.from_dict(cfg.pii.regex_overrides).to_overrides()

    out_dir = Path(output_dir) if output_dir else cfg.report.output_dir

    return ScanConfig(
        table=tbl,
        equation_mode=cfg.pii.equation_mode,
        thresholds=cfg.pii.thresholds,
        triage_threshold=cfg.profiling.triage_threshold,
        run_pii=run_pii,
        run_profile=run_profile,
        run_quality=run_quality,
        profiler_engine=cfg.profiling.engine,
        generate_ge_docs=cfg.quality.generate_ge_docs,
        validate_contracts=cfg.contract.validate,
        output_dir=out_dir,
        automerge=automerge,
        pii_columns=cfg.pii.selected_columns,
        pii_engines=cfg.pii.engines,
        pii_regex_confidence=cfg.pii.thresholds.presidio_min,
        pii_gliner_confidence=cfg.pii.thresholds.gliner_min,
        pii_llm_confidence=cfg.pii.thresholds.llm_min,
        pii_gliner_always_run=cfg.pii.gliner_always_run,
        pii_regex_overrides=regex_overrides,
        ner_config=NERConfig(
            type=cfg.pii.ner.type,
            model_path=cfg.pii.ner.model_path,
            labels=list(cfg.pii.ner.labels or []),
            device=cfg.pii.ner.device,
            threshold=cfg.pii.ner.threshold,
            batch_size=cfg.pii.ner.batch_size,
            always_run=cfg.pii.ner_always_run(),
        ),
        gliner_config=GlinerConfig(
            model_id=cfg.pii.gliner.model_id,
            device=cfg.pii.gliner.device,
            batch_size=cfg.pii.gliner.batch_size,
        ),
        llm_config=llm_cfg,
        masking_roles=cfg.masking.roles_path,
        use_phonenumbers=cfg.pii.use_phonenumbers,
        evidence_bundle_enabled=cfg.report.evidence_bundle.enabled,
        evidence_bundle_sample_mode=cfg.report.evidence_bundle.sample_mode,
        evidence_bundle_sample_n=cfg.report.evidence_bundle.sample_n,
        evidence_bundle_top_values_n=cfg.report.evidence_bundle.top_values_n,
        evidence_bundle_format_masks_n=cfg.report.evidence_bundle.format_masks_n,
        evidence_bundle_numeric_histogram_bins=cfg.report.evidence_bundle.numeric_histogram_bins,
        evidence_bundle_length_histogram_bins=cfg.report.evidence_bundle.length_histogram_bins,
        evidence_bundle_include_profile_groups=(
            list(cfg.report.evidence_bundle.include_profile_groups) or None
        ),
    )


def from_scan_config(sc: ScanConfig) -> "RedibisConfig":
    """Build ``RedibisConfig`` from a legacy ``ScanConfig``."""
    from redibis.config import (
        ContractConfig,
        GlinerConfig as CfgGlinerConfig,
        LLMConfig as CfgLLMConfig,
        MaskingConfig,
        NERConfig as CfgNERConfig,
        PIIConfig,
        ProfilingConfig,
        QualityConfig,
        RedibisConfig,
        ReportConfig,
    )
    from redibis.pii.regex_overrides import RegexSet

    scan_types: list[str] = []
    if getattr(sc, "run_profile", True):
        scan_types.append("profile")
    if sc.run_quality:
        scan_types.append("quality")
    if sc.run_pii:
        scan_types.append("pii")
    if not scan_types:
        scan_types = ["profile", "quality", "pii"]

    thresholds = sc.thresholds or Thresholds(
        presidio_min=sc.pii_regex_confidence or 0.80,
        gliner_min=sc.pii_gliner_confidence or 0.70,
        llm_min=sc.pii_llm_confidence or 0.82,
    )

    gliner = CfgGlinerConfig()
    if sc.gliner_config:
        gliner = CfgGlinerConfig(
            model_id=sc.gliner_config.model_id,
            device=sc.gliner_config.device,
            batch_size=sc.gliner_config.batch_size,
        )

    ner = sc.ner_config or CfgNERConfig()

    llm = CfgLLMConfig()
    if sc.llm_config:
        llm = CfgLLMConfig(
            provider=sc.llm_config.provider,
            model_name=sc.llm_config.model_name,
            api_key=sc.llm_config.api_key,
            endpoint_url=sc.llm_config.endpoint_url,
            temperature=sc.llm_config.temperature,
            enabled=bool(sc.llm_config.api_key),
        )

    regex_overrides = None
    if sc.pii_regex_overrides is not None:
        regex_overrides = RegexSet.from_overrides(sc.pii_regex_overrides).to_dict()

    return RedibisConfig(
        table=sc.table,
        scan_types=scan_types,
        profiling=ProfilingConfig(
            engine=getattr(sc, "profiler_engine", "great_expectations"),
            triage_threshold=sc.triage_threshold,
        ),
        quality=QualityConfig(
            generate_ge_docs=sc.generate_ge_docs,
        ),
        pii=PIIConfig(
            engines=sc.pii_engines,
            equation_mode=sc.equation_mode,
            thresholds=thresholds,
            ner=ner,
            gliner=gliner,
            llm=llm,
            gliner_always_run=sc.pii_gliner_always_run,
            regex_overrides=regex_overrides,
            selected_columns=sc.pii_columns,
        ),
        masking=MaskingConfig(roles_path=sc.masking_roles if isinstance(sc.masking_roles, str) else None),
        contract=ContractConfig(
            automerge=sc.automerge,
            validate=sc.validate_contracts,
        ),
        report=ReportConfig(output_dir=Path(sc.output_dir)),
    )


# Back-compat re-export -- ScanConfig's real home is redibis.scan.config.
from redibis.scan.config import ScanConfig  # noqa: E402,F401
