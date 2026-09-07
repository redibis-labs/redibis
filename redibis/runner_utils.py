"""
Shared utilities for legacy Workflow A / B pipeline runners.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator

import pandas as pd

from redibis.quality.sampling import PandasTableSampler, SamplingConfig


def utc_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class StepTiming:
    """Holds the elapsed time (ms) for a pipeline step."""
    elapsed_ms: float = 0.0


@contextmanager
def timed_step() -> Generator[StepTiming, None, None]:
    """Context manager that measures wall-clock time for a step."""
    timing = StepTiming()
    start = time.monotonic()
    try:
        yield timing
    finally:
        timing.elapsed_ms = (time.monotonic() - start) * 1000.0


def load_sample(
    table: str,
    output_dir: Path,
    strategy: str = "partition_picker",
) -> pd.DataFrame:
    """
    Load a pre-sampled parquet file from *output_dir*.

    The expected path is ``{output_dir}/{db_table}_sample.parquet``.
    """
    sample_path = output_dir / f"{table.replace('.', '_')}_sample.parquet"
    if not sample_path.exists():
        raise FileNotFoundError(
            f"Sample file not found: {sample_path}. "
            "Provide a pre-sampled parquet file at that path, "
            "or use the Spark-based TableSampler for live sampling."
        )
    cfg = SamplingConfig(strategy=strategy)
    return PandasTableSampler(cfg).from_parquet(str(sample_path))
