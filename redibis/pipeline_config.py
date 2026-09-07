"""
Legacy pipeline configuration dataclasses (Workflow A / B).

``RedibisConfig`` in ``redibis.config`` is the canonical configuration surface;
these types remain for the standalone PII/GE pipeline runners.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from redibis.config import ConfigError, RedibisConfig
from redibis.pii.thresholds import DEFAULT_EQUATION, Thresholds


@dataclass
class PipelineConfig:
    """Configuration for the PII detection pipeline (Workflow B)."""

    table:             str
    output_dir:        Path          = Path("./reports")
    equation:          str           = DEFAULT_EQUATION
    strategy:          str           = "partition_picker"
    thresholds:        Thresholds    = field(default_factory=Thresholds)
    enable_llm:        bool          = False
    runs_bucket:       str           = "pii-reports"
    contracts_bucket:  str           = "pii-contracts"
    generate_ge_docs:  bool          = False

    def __post_init__(self):
        self.output_dir = Path(self.output_dir)


@dataclass
class GEPipelineConfig:
    """Configuration for the GE-only quality pipeline (Workflow A)."""

    table:            str
    output_dir:       Path  = Path("./reports")
    strategy:         str   = "partition_picker"
    runs_bucket:      str   = "pii-reports"
    contracts_bucket: str   = "pii-contracts"

    def __post_init__(self):
        self.output_dir = Path(self.output_dir)


def to_pipeline_config(cfg: RedibisConfig, *, table: Optional[str] = None) -> PipelineConfig:
    """Project ``RedibisConfig`` → legacy ``PipelineConfig``."""
    tbl = table or cfg.table
    if not tbl:
        raise ConfigError("table is required")
    return PipelineConfig(
        table=tbl,
        output_dir=cfg.report.output_dir,
        equation=cfg.pii.equation_mode,
        thresholds=cfg.pii.thresholds,
        enable_llm=cfg.pii.llm.enabled,
        runs_bucket=cfg.storage.runs_bucket,
        contracts_bucket=cfg.storage.contracts_bucket,
        generate_ge_docs=cfg.quality.generate_ge_docs,
    )


def to_ge_pipeline_config(cfg: RedibisConfig, *, table: Optional[str] = None) -> GEPipelineConfig:
    """Project ``RedibisConfig`` → legacy ``GEPipelineConfig``."""
    tbl = table or cfg.table
    if not tbl:
        raise ConfigError("table is required")
    return GEPipelineConfig(
        table=tbl,
        output_dir=cfg.report.output_dir,
        runs_bucket=cfg.storage.runs_bucket,
        contracts_bucket=cfg.storage.contracts_bucket,
    )
