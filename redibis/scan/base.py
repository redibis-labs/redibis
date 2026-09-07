"""Scan orchestrator — template-method pipeline with independently callable phases."""

from __future__ import annotations

import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import pandas as pd

from redibis.config import RedibisConfig
from redibis.contracts.type_inference import dtype_map_from_dataframe
from redibis.profiling.base import ProfileResult
from redibis.quality.rule_set import QualityRuleSet
from redibis.quality.sampling import PandasTableSampler
from redibis.scan.pii_phase import build_pii_contract
from redibis.scan.pii_scanner import PIIScanner
from redibis.scan.quality_phase import run_quality_phase
from redibis.scan.types import ScanRunResult
from redibis.scan.contract_writer import ScanContractWriter
from redibis.services import pipeline
from redibis.scan.config import ScanConfig

log = logging.getLogger(__name__)

class Scan:
    """
    In-memory scan **engine** (template method).

    This is the low-level engine, NOT the service. ``run()`` performs profiling /
    quality / PII in memory and returns an ``EngineScanResult``; it does no file/S3
    I/O (delegated to ``ReportBundle.flush`` and ``ScanContractWriter.persist``).
    For a file -> S3 -> contract job use ``redibis.services.scan_service.ScanService``.
    Preferred library entry: the facades ``PIIScan`` / ``QualityScan`` / ``ProfileScan``.
    See ``docs/ARCHITECTURE_NAMING_REVIEW.md`` section 2.
    """

    def __init__(self, config: ScanConfig, *, redibis_config: Optional[RedibisConfig] = None):
        self.config = config
        self._redibis_config = redibis_config
        self._profile: Optional[ProfileResult] = None
        self._pii_scanner = PIIScanner(config)
        if redibis_config is not None:
            self._pii_scanner._redibis_config = redibis_config  # type: ignore[attr-defined]

    @classmethod
    def from_redibis(cls, config: RedibisConfig) -> Scan:
        from redibis.services.scan_service import to_scan_config

        return cls(to_scan_config(config), redibis_config=config)

    def profile(
        self,
        df: pd.DataFrame,
        *,
        tbl_name: str,
        log_fn: Optional[Callable[[str], None]] = None,
    ) -> ProfileResult:
        if log_fn:
            log_fn("Profiling data...")
        from redibis.config import ProfilingConfig

        rb = self._redibis_config
        profiling = (
            rb.profiling
            if rb is not None
            else ProfilingConfig(
                engine=self.config.profiler_engine,
                triage_threshold=self.config.triage_threshold,
            )
        )
        self._profile = pipeline.profile_dataframe(
            df,
            tbl_name,
            self.config.triage_threshold,
            profiling=profiling,
            source=rb.source if rb is not None else None,
            table=self.config.table,
            domain=rb.memory.domain if rb is not None else "",
        )
        return self._profile

    def suggest_quality_rules(self) -> QualityRuleSet:
        if self._profile is None:
            raise RuntimeError("call profile() before suggest_quality_rules()")
        return self._profile.suggested_rules

    def detect_pii(
        self,
        df: pd.DataFrame,
        *,
        log_fn: Optional[Callable[[str], None]] = None,
    ) -> list:
        if log_fn:
            log_fn("Running PII detection...")
        try:
            return self._pii_scanner.detect(df, progress_callback=log_fn)
        except (ImportError, NotImplementedError):
            msg = (
                "PII detection engines not available. "
                "Install with: pip install redibis[ner]"
            )
            if log_fn:
                log_fn(msg)
            else:
                log.warning(msg)
            return []

    def run(
        self,
        df: pd.DataFrame,
        *,
        run_id: Optional[str] = None,
        run_dir: Optional[Path] = None,
        log_fn: Optional[Callable[[str], None]] = None,
    ) -> ScanRunResult:
        cfg = self.config
        run_id = run_id or datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
        started = datetime.now(timezone.utc)
        db_name, tbl_name = pipeline.split_table(cfg.table)

        result = ScanRunResult(
            run_id=run_id,
            table=cfg.table,
            session_id=cfg.session_id,
            started_at=started.isoformat(),
        )

        ge_root: Optional[Path] = None
        if run_dir is not None:
            ge_root = run_dir / "ge_project"
        elif cfg.generate_ge_docs:
            ge_root = Path(tempfile.mkdtemp(prefix="redibis_ge_"))

        try:
            if log_fn:
                log_fn(f"Starting scan for {cfg.table}")
                log_fn(f"Run ID: {run_id}")

            if cfg.sampling_config:
                if log_fn:
                    log_fn("Sampling data...")
                t0 = datetime.now(timezone.utc).isoformat()
                sampler = PandasTableSampler(cfg.sampling_config)
                df = sampler.from_dataframe(df)
                result.phase_timings["sampling"] = {
                    "started_at": t0, "finished_at": datetime.now(timezone.utc).isoformat(),
                }

            result.total_rows = len(df)
            result.total_columns = len(df.columns)
            col_dtypes = dtype_map_from_dataframe(df)
            result.col_dtypes = col_dtypes

            profile: Optional[ProfileResult] = None
            if cfg.run_profile:
                t0 = datetime.now(timezone.utc).isoformat()
                profile = self.profile(df, tbl_name=tbl_name, log_fn=log_fn)
                result.phase_timings["profiling"] = {
                    "started_at": t0, "finished_at": datetime.now(timezone.utc).isoformat(),
                }
                result.profile = profile
            elif self._profile is not None:
                profile = self._profile
                result.profile = profile

            if cfg.run_quality and profile is not None:
                t0 = datetime.now(timezone.utc).isoformat()
                effective_run_dir = run_dir or Path(tempfile.mkdtemp(prefix="redibis_run_"))
                quality_contract, qa, quality_results, stats = run_quality_phase(
                    df,
                    cfg,
                    profile,
                    db_name=db_name,
                    tbl_name=tbl_name,
                    run_dir=effective_run_dir,
                    rule_set=cfg.quality_rule_set,
                    log_fn=log_fn,
                )
                result.phase_timings["quality"] = {
                    "started_at": t0, "finished_at": datetime.now(timezone.utc).isoformat(),
                }
                result.quality_contract = quality_contract
                result.quality_gatekeeper = qa
                result.quality_results = quality_results
                result.quality_expectations = stats.get("total", 0)
                result.quality_passed = stats.get("passed", 0)
                result.quality_failed = stats.get("failed", 0)

            if cfg.run_pii:
                t0 = datetime.now(timezone.utc).isoformat()
                detections = self.detect_pii(df, log_fn=log_fn)
                result.phase_timings["pii"] = {
                    "started_at": t0, "finished_at": datetime.now(timezone.utc).isoformat(),
                }
                result.pii_detections = detections
                result.pii_columns_scanned = len(detections)
                result.pii_columns_detected = sum(1 for d in detections if d.detected)
                result.arabic_aware_columns = sum(
                    1 for d in detections if d.arabic_aware
                )
                result.pii_contract = build_pii_contract(
                    detections,
                    db_name=db_name,
                    tbl_name=tbl_name,
                    config=cfg,
                    run_id=run_id,
                    col_dtypes=col_dtypes,
                )

            if col_dtypes:
                from redibis.contracts.schema_base import build_schema_base

                profiles = getattr(profile, "column_profiles", None) if profile else None
                result.schema_contract = build_schema_base(
                    cfg.table,
                    col_dtypes,
                    df=df,
                    profiles=profiles,
                )
                if profile and profile.source_metadata:
                    from redibis.profiling.metadata_tiers import apply_catalog_props_to_partial

                    result.schema_contract = apply_catalog_props_to_partial(
                        result.schema_contract, profile.source_metadata,
                    )

            draft = ScanContractWriter.build(result)
            result.contract_draft = draft

            rb = self._redibis_config
            if rb is not None and rb.classification.enabled:
                from redibis.scan.classification_phase import (
                    contract_for_classification,
                    run_classification_phase,
                )
                class_contract = contract_for_classification(
                    pii_partial=result.pii_contract,
                    quality_partial=result.quality_contract,
                )
                if class_contract is not None:
                    result.classification = run_classification_phase(
                        class_contract,
                        cfg.table,
                        rb,
                    )
                    if log_fn:
                        log_fn(
                            f"Classification — {len(result.classification)} columns tagged"
                        )

            result.status = "success"
            result.completed_at = datetime.now(timezone.utc).isoformat()
            result.duration_seconds = (
                datetime.now(timezone.utc) - started
            ).total_seconds()
            if log_fn:
                log_fn(
                    f"Scan complete — {result.quality_passed}/"
                    f"{result.quality_expectations} quality, "
                    f"{result.pii_columns_detected} PII"
                )

        except Exception as exc:
            result.status = "failed"
            result.error = str(exc)
            result.completed_at = datetime.now(timezone.utc).isoformat()
            if log_fn:
                log_fn(f"ERROR: {exc}")
            log.exception("Scan failed for %s: %s", cfg.table, exc)

        return result
