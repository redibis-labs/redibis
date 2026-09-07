"""
redibis.profiling.base
======================
Engine-neutral profiling result and strategy ABC.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional

import pandas as pd

from redibis.config import ProfilingConfig
from redibis.models import ColumnProfile
from redibis.quality.rule_set import QualityRuleSet


@dataclass
class ProfileResult:
    """
    Neutral output of a profiling pass.

    The GE compat shim (``.expectations``, ``export_*``) delegates to the wrapped
    ``QualityProfiler`` stored in ``raw["ge_profiler"]`` so existing web/session
    callers keep working without importing GE types.
    """

    column_profiles: list[ColumnProfile]
    arabic_columns: dict[str, float]
    triage_signals: list[ColumnProfile]
    suggested_rules: QualityRuleSet
    native_report_html: Optional[str] = None
    fingerprints: list = field(default_factory=list)
    structural_fingerprints: list = field(default_factory=list)
    source_metadata: Optional[Any] = None
    raw: dict = field(default_factory=dict)

    @property
    def expectations(self) -> list:
        ge = self.raw.get("ge_profiler")
        if ge is None:
            return []
        return getattr(ge, "expectations", [])

    @property
    def profiler(self):
        """Legacy handle — prefer ``expectations`` / ``export_*`` on this object."""
        return self.raw.get("ge_profiler")

    def export_triage_report(
        self,
        output_filename: str = "triage_report.html",
        open_browser: bool = False,
    ) -> "ProfileResult":
        ge = self.raw.get("ge_profiler")
        if ge is not None:
            ge.export_triage_report(
                output_filename=output_filename,
                open_browser=open_browser,
            )
        elif self.triage_signals:
            from redibis.profiling.triage import write_triage_report

            write_triage_report(
                self.triage_signals,
                output_filename,
                open_browser=open_browser,
            )
        return self

    def export_interactive_review(
        self,
        output_filename: str = "interactive_review.html",
        open_browser: bool = False,
    ) -> "ProfileResult":
        ge = self.raw.get("ge_profiler")
        if ge is not None:
            ge.export_interactive_review(
                output_filename=output_filename,
                open_browser=open_browser,
            )
        elif self.native_report_html:
            Path(output_filename).write_text(
                self.native_report_html, encoding="utf-8",
            )
        return self

    def export_html_review(
        self,
        output_filename: str = "profiler_draft_review.html",
        open_browser: bool = False,
    ) -> "ProfileResult":
        ge = self.raw.get("ge_profiler")
        if ge is not None:
            ge.export_html_review(
                output_filename=output_filename,
                open_browser=open_browser,
            )
        return self

    def drop_rules_by_index(self, indices_to_drop: List[int]) -> "ProfileResult":
        ge = self.raw.get("ge_profiler")
        if ge is not None:
            ge.drop_rules_by_index(indices_to_drop)
        return self


class Profiler(ABC):
    """Strategy interface — swap GE / OpenMetadata via config."""

    name: str

    def __init__(self, config: ProfilingConfig) -> None:
        self.config = config

    @abstractmethod
    def profile(self, df: pd.DataFrame, *, dataset_name: str) -> ProfileResult:
        """Run profiling and return a neutral ``ProfileResult``."""
