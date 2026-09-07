"""
Thin scan facades — ProfileScan, QualityScan, PIIScan over the unified ``Scan`` core.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Union

import pandas as pd

from redibis.config import RedibisConfig
from redibis.services.scan_service import to_scan_config
from redibis.models import PIIDetection
from redibis.pii.report_writer import render_pii_detection_report_html
from redibis.profiling.base import ProfileResult
from redibis.scan.base import Scan
from redibis.scan.contract_writer import ScanContractWriter
from redibis.scan.report_bundle import ReportBundle
from redibis.scan.types import ContractPersistResult, ScanRunResult
from redibis.scan.config import ScanConfig


def _flush_dir(config: ScanConfig, run_id: str, run_dir: Optional[Path]) -> Path:
    if run_dir is not None:
        return Path(run_dir)
    return Path(getattr(config, "output_dir", None) or "./reports") / run_id


def _mint_run_id(run_id: Optional[str]) -> str:
    return run_id or datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")


def _run_and_flush(
    sc: ScanConfig,
    df: pd.DataFrame,
    *,
    run_id: Optional[str],
    run_dir: Optional[Path],
    flush: bool,
    log: Optional[Callable[[str], None]],
) -> tuple[ScanRunResult, dict[str, str]]:
    rid = _mint_run_id(run_id)
    dest = _flush_dir(sc, rid, run_dir)
    from redibis.telemetry.llm_evidence import llm_evidence_recorder

    with llm_evidence_recorder(
        run_dir=dest,
        run_id=rid,
        table=sc.table,
        execution_mode="deterministic",
        config=sc,
    ):
        run = Scan(sc).run(df, run_id=rid, run_dir=run_dir, log_fn=log)
        artifacts: dict[str, str] = {}
        if flush:
            dest = _flush_dir(sc, run.run_id, run_dir)
            try:
                artifacts = ReportBundle(run, sc).flush(dest, df=df)
            except Exception as exc:
                run.evidence_warnings.append({"phase": "flush", "error": str(exc)})
                if log:
                    log(f"Warning: evidence flush failed: {exc}")
        return run, artifacts


def _scan_config_for_types(
    config: Union[RedibisConfig, ScanConfig],
    scan_types: list[str],
) -> ScanConfig:
    if isinstance(config, ScanConfig):
        run_pii = "pii" in scan_types
        run_profile = "profile" in scan_types or "quality" in scan_types
        run_quality = "quality" in scan_types
        return replace(
            config,
            run_pii=run_pii,
            run_profile=run_profile,
            run_quality=run_quality,
        )
    cfg = RedibisConfig.from_dict(config.to_dict()) if isinstance(config, RedibisConfig) else config
    cfg.scan_types = scan_types
    return to_scan_config(cfg)


@dataclass
class ProfileScanResult:
    table: str
    run_id: str
    profile: Optional[ProfileResult] = None
    native_report_html: Optional[str] = None
    artifacts: dict[str, str] = field(default_factory=dict)
    status: str = "pending"
    error: Optional[str] = None


@dataclass
class QualityScanResult:
    table: str
    run_id: str
    profile: Optional[ProfileResult] = None
    quality_contract: Optional[dict] = None
    quality_passed: int = 0
    quality_expectations: int = 0
    artifacts: dict[str, str] = field(default_factory=dict)
    status: str = "pending"
    error: Optional[str] = None


@dataclass
class PIIScanResult:
    table: str
    run_id: str
    detections: list[PIIDetection] = field(default_factory=list)
    flagged_columns: list[str] = field(default_factory=list)
    contract_partial: Optional[dict] = None
    report_html: Optional[str] = None
    artifacts: dict[str, str] = field(default_factory=dict)
    status: str = "pending"
    error: Optional[str] = None

    @property
    def pii_columns_detected(self) -> int:
        return sum(1 for d in self.detections if d.detected)


class ProfileScan:
    """Profile-only facade: ``Scan(scan_types=['profile']).run(df)``."""

    def __init__(self, config: Union[RedibisConfig, ScanConfig]):
        self.config = config

    def run(
        self,
        df: pd.DataFrame,
        *,
        run_id: Optional[str] = None,
        run_dir: Optional[Path] = None,
        flush: bool = True,
        log: Optional[Callable[[str], None]] = None,
    ) -> ProfileScanResult:
        sc = _scan_config_for_types(self.config, ["profile"])
        run, artifacts = _run_and_flush(
            sc, df, run_id=run_id, run_dir=run_dir, flush=flush, log=log,
        )
        profile = run.profile
        return ProfileScanResult(
            table=run.table,
            run_id=run.run_id,
            profile=profile,
            native_report_html=profile.native_report_html if profile else None,
            artifacts=artifacts,
            status=run.status,
            error=run.error,
        )


class QualityScan:
    """Quality facade: profile + GE gatekeeper (discovery always uses GE)."""

    def __init__(self, config: Union[RedibisConfig, ScanConfig]):
        self.config = config

    def run(
        self,
        df: pd.DataFrame,
        *,
        run_id: Optional[str] = None,
        run_dir: Optional[Path] = None,
        flush: bool = True,
        log: Optional[Callable[[str], None]] = None,
    ) -> QualityScanResult:
        sc = _scan_config_for_types(self.config, ["profile", "quality"])
        run, artifacts = _run_and_flush(
            sc, df, run_id=run_id, run_dir=run_dir, flush=flush, log=log,
        )
        return QualityScanResult(
            table=run.table,
            run_id=run.run_id,
            profile=run.profile,
            quality_contract=run.quality_contract,
            quality_passed=run.quality_passed,
            quality_expectations=run.quality_expectations,
            artifacts=artifacts,
            status=run.status,
            error=run.error,
        )


class PIIScan:
    """PII-only facade over the shared detector + equation pipeline."""

    def __init__(self, config: Union[RedibisConfig, ScanConfig]):
        self.config = config

    def run(
        self,
        df: pd.DataFrame,
        *,
        run_id: Optional[str] = None,
        run_dir: Optional[Path] = None,
        flush: bool = True,
        log: Optional[Callable[[str], None]] = None,
    ) -> PIIScanResult:
        sc = _scan_config_for_types(self.config, ["pii"])
        run, artifacts = _run_and_flush(
            sc, df, run_id=run_id, run_dir=run_dir, flush=flush, log=log,
        )
        report_html: Optional[str] = None
        if run.pii_detections:
            report_html = render_pii_detection_report_html(
                run.pii_detections, run.table,
            )
        if report_html is None:
            html_path = artifacts.get("pii_detections_html")
            if html_path and Path(html_path).exists():
                report_html = Path(html_path).read_text(encoding="utf-8")
        flagged = [d.column for d in run.pii_detections if d.detected]
        return PIIScanResult(
            table=run.table,
            run_id=run.run_id,
            detections=list(run.pii_detections),
            flagged_columns=flagged,
            contract_partial=run.pii_contract,
            report_html=report_html,
            artifacts=artifacts,
            status=run.status,
            error=run.error,
        )

    def persist_contract(
        self,
        result: PIIScanResult,
        *,
        sub_store,
        store=None,
        merger=None,
        validate: bool = True,
        automerge: bool = False,
    ) -> ContractPersistResult:
        """Write PII subcontract (+ optional automerge) via ``ScanContractWriter``."""
        if result.contract_partial is None:
            return ContractPersistResult()
        from redibis.scan.types import ContractDraft

        draft = ContractDraft(
            table=result.table,
            run_id=result.run_id,
            pii_partial=result.contract_partial,
            pii_summary={
                "columns_scanned": len(result.detections),
                "columns_detected": result.pii_columns_detected,
            },
        )
        sc = _scan_config_for_types(self.config, ["pii"])
        if automerge:
            sc = replace(sc, automerge="pii")
        return ScanContractWriter.persist(
            draft,
            sub_store=sub_store,
            store=store,
            merger=merger,
            config=sc,
            validate=validate,
        )
