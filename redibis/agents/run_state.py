"""Per-table pipeline execution state shared across node steps."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import pandas as pd

from redibis.profiling.base import ProfileResult
from redibis.scan.base import Scan
from redibis.scan.types import ScanRunResult
from redibis.scan.config import ScanConfig


@dataclass
class TableRunState:
    """Mutable state for one table as the pipeline executes in order."""

    table: str
    df: Optional[pd.DataFrame] = None
    scan: Optional[Scan] = None
    scan_config: Optional[ScanConfig] = None
    profile: Optional[ProfileResult] = None
    scan_result: Optional[ScanRunResult] = None
    run_id: str = ""
    approval_granted: bool = False
    extras: dict[str, Any] = field(default_factory=dict)

    def ensure_scan(self, config: ScanConfig, *, redibis_config: Any = None) -> Scan:
        if self.scan is None or self.scan_config != config:
            self.scan = Scan(config, redibis_config=redibis_config)
            self.scan_config = config
        return self.scan
